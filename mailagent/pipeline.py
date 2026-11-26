"""De workflow: ophalen -> lokale regels -> sorteren -> uitwerken -> wachtrij -> goedkeuren."""

from __future__ import annotations

import fcntl
import json
import logging
import re
from contextlib import contextmanager
from email.utils import parseaddr
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config, anonimiseer, mag_naar_claude, sorteer_met_regels
from .llm import LLM, LLMFout
from .models import UITWERK_ACTIES, Email, OpenPunt, Sortering
from .store import OPEN_STATUSSEN, Store

log = logging.getLogger("mailagent")

OPEN_PLEK = re.compile(r"\[\s*(FILL IN|AANVULLEN)", re.IGNORECASE)   # plek die Claude voor jou openliet
HELE_PLEK = re.compile(r"\[\s*(?:FILL IN|AANVULLEN)\b[^\]\n]*\]", re.IGNORECASE)


def schone_open_punten(concept: str | None, punten: list) -> list[OpenPunt]:
    """De open punten van Claude nalopen, zodat elke knop iets in het concept kan invullen.
    Een punt bij een plek die niet in het concept staat, vervalt (of krijgt de enige plek zonder punt). Een
    [FILL IN] zonder punt krijgt alsnog een invulveld, zodat je nooit in de tekst hoeft te zoeken."""
    concept = concept or ""
    plekken = HELE_PLEK.findall(concept)
    uit: list[OpenPunt] = []
    los = []
    for p in punten[:6]:
        p = OpenPunt.model_validate(p) if not isinstance(p, OpenPunt) else p.model_copy(deep=True)
        if not (p.vraag or "").strip():
            continue
        if p.soort not in ("keuze", "invullen", "hele_mail"):
            p.soort = "keuze" if p.opties else "invullen"
        p.opties = [o for o in p.opties if (o.label or "").strip()][:4]
        if p.soort == "hele_mail":
            p.plek = None
            if len(p.opties) < 2:
                continue
            for o in p.opties:
                o.tekst = None
        else:
            if p.soort == "keuze" and not p.opties:
                p.soort = "invullen"
            if p.soort == "invullen":
                p.opties = []
            for o in p.opties:
                if o.tekst is None:
                    o.tekst = o.label
            if not p.plek or p.plek not in concept:
                los.append(p)
                continue
        if p.suggestie is not None and not (0 <= p.suggestie < len(p.opties)):
            p.suggestie = None
        uit.append(p)
    bezet = {p.plek for p in uit if p.plek}
    vrij = [pl for pl in dict.fromkeys(plekken) if pl not in bezet]
    # Claude schreef de plek net anders over: koppelen aan de vrije plek met de meeste woorden gemeen
    woorden = lambda t: set(re.findall(r"\w{3,}", re.sub(r"(?i)fill in|aanvullen", "", t or "").lower()))
    for p in los:
        if not vrij:
            break
        scores = [(len(woorden(p.plek) & woorden(pl)), pl) for pl in vrij]
        beste, pl = max(scores, key=lambda x: x[0])
        if beste == 0 and not (len(los) == 1 and len(vrij) == 1):
            continue
        p.plek = pl
        vrij.remove(pl)
        uit.append(p)
    for pl in vrij:
        omschrijving = re.sub(r"^\[\s*(?:FILL IN|AANVULLEN)\s*:?\s*|\]$", "", pl, flags=re.IGNORECASE).strip()
        uit.append(OpenPunt(vraag=f"Fill in: {omschrijving}" if omschrijving else "Fill in", soort="invullen", plek=pl))
    volgorde = {pl: concept.find(pl) for pl in plekken}
    return sorted(uit, key=lambda p: volgorde.get(p.plek, -1) if p.plek else -1)


def stijlvoorbeelden(cfg: Config, store: Store, mail: Email, project: str, aantal: int = 3) -> list[dict]:
    """Eerdere antwoorden van de eigenaar: eerst aan dezelfde afzender, dan binnen hetzelfde project.

    Antwoorden aan afzenders op de lijst 'nooit naar Claude' gaan nooit mee.
    """
    adres = afzender_adres(mail.van)
    zelfde, project_ = [], []
    for r in store.eigen_antwoorden():
        voorbeeld_mail = Email(bron="", bron_id="", van=r["van"] or "", aan="", onderwerp=r["onderwerp"] or "",
                               datum=mail.datum, tekst="")
        if not mag_naar_claude(cfg, voorbeeld_mail):
            continue
        v = {"van": afzender_adres(r["van"] or ""), "onderwerp": r["onderwerp"] or "",
             "antwoord": anonimiseer(cfg, r["eigen_antwoord"])}
        if afzender_adres(r["van"] or "") == adres:
            zelfde.append(v)
        elif r["project"] == project:
            project_.append(v)
    return (zelfde + project_)[:aantal]


def verwerk_mail(cfg: Config, store: Store, llm: LLM, mail: Email, agenda=None) -> int:
    """Verwerkt één mail en geeft het id van het item in de wachtrij terug.

    De mail wordt pas als verwerkt opgeslagen samen met zijn item (zie Store.nieuw_item)."""
    vast_project, sortering = sorteer_met_regels(cfg, mail)
    belangrijk = is_belangrijk(cfg, store, mail.van)

    geleerd = store.afzender_regel(afzender_adres(mail.van))
    if sortering is None and geleerd is not None and not belangrijk:
        s = Sortering(project=geleerd["project"] or vast_project or "overig", actie=geleerd["actie"],
                      prioriteit="laag", deadline=None, samenvatting=mail.onderwerp,
                      reden="sender is on your list of senders to skip")
        return store.nieuw_item(mail, s, None, "genegeerd")

    if not mag_naar_claude(cfg, mail):
        s = Sortering(project=vast_project or "overig", actie="antwoorden", prioriteit="hoog" if belangrijk else "normaal",
                      deadline=None, samenvatting=mail.onderwerp, reden="on the list of senders never sent to Claude")
        return store.nieuw_item(mail, s, None, "handmatig")

    if cfg.bulk_overslaan and mail.extra.get("bulk") and vast_project is None and sortering is None and not belangrijk:
        s = Sortering(project="overig", actie="negeren", prioriteit="laag", deadline=None,
                      samenvatting=mail.onderwerp, reden="newsletter or advertising (has an unsubscribe link); not sent to Claude")
        return store.nieuw_item(mail, s, None, "genegeerd")

    tekst = anonimiseer(cfg, mail.tekst)
    uitwerking, context, verzoek = None, [], None
    try:
        if sortering is None:
            sortering = llm.sorteer(mail, tekst, vast_project)
        if sortering.actie in UITWERK_ACTIES:
            extra, context = achtergrond(cfg, store, sleutel=mail.sleutel, van=mail.van, onderwerp=mail.onderwerp,
                                         thread_id=mail.thread_id, verwijzingen=mail.extra.get("verwijzingen"),
                                         datum=mail.datum)
            verzoek, check = afspraak_nakijken(cfg, llm, agenda, mail, tekst, sortering.actie, extra or None)
            if check:
                context.append({"soort": "agenda", **check["telling"]})
            uitwerking = llm.werk_uit(mail, tekst, sortering, stijlvoorbeelden(cfg, store, mail, sortering.project),
                                      achtergrond=extra or None, agenda_check=check["blok"] if check else None)
            uitwerking.open_punten = schone_open_punten(uitwerking.concept_antwoord, uitwerking.open_punten)
    except Exception as e:  # ook een kapot antwoord of een onverwachte fout: de mail komt bij 'zelf doen'
        soort = "Claude-fout" if isinstance(e, LLMFout) else f"onverwachte fout ({type(e).__name__})"
        log.warning("%s bij %s: %s", soort, mail.sleutel, e)
        s = sortering or Sortering(project=vast_project or "overig", actie="antwoorden", prioriteit="normaal",
                                   deadline=None, samenvatting=mail.onderwerp, reden="not sorted")
        return store.nieuw_item(mail, s, None, "fout", opmerking=f"{soort}: {e}"[:500])
    if belangrijk:   # nooit missen: altijd in Te doen, bovenaan
        sortering = sortering.model_copy(update={"prioriteit": "hoog",
                                                 "reden": f"{sortering.reden} · belangrijke afzender".strip(" ·")})
    if sortering.actie in UITWERK_ACTIES:
        item_id = store.nieuw_item(mail, sortering, uitwerking, "wacht")
        if context:
            store.zet_context(item_id, context)
        if verzoek:   # Afspraak plannen hoeft de mail dan niet opnieuw aan Claude voor te leggen
            store.zet_afspraak_verzoek(item_id, verzoek)
        return item_id
    if belangrijk:
        return store.nieuw_item(mail, sortering, None, "wacht")
    status = "genegeerd" if sortering.actie == "negeren" else "ter_info"
    return store.nieuw_item(mail, sortering, None, status)


# Een mail waarin iemand wil afspreken of een moment noemt: eerst de agenda nakijken, zodat het concept niets toezegt
# op een moment dat je niet kunt. Alleen de nieuwe tekst telt, niet het geciteerde origineel.
AFSPRAAK_WOORDEN = re.compile(
    r"\b(afspraak|afspreken|afspreekt|meeting|meet|call|bellen|koffie|coffee|kennismak\w*|overleg|gesprek|zoom|teams|"
    r"beschikbaar|available|availability|schikt|uitkomt|past het|inplannen|plannen|schedule)\b", re.IGNORECASE)
MOMENT_WOORDEN = re.compile(
    r"\b(maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|morgen|overmorgen|tomorrow|volgende week|next week)\b|\b\d{1,2}[.:]\d{2}\b|\b\d{1,2}\s*(uur|u|am|pm)\b|"
    r"\b\d{1,2}\s+(jan|feb|mrt|maart|apr|mei|may|jun|jul|aug|sep|okt|oct|nov|dec)", re.IGNORECASE)


def lijkt_afspraak(actie: str, tekst: str) -> bool:
    eigen = _eigen_tekst(tekst or "")
    return actie == "agenda" or bool(AFSPRAAK_WOORDEN.search(eigen) and MOMENT_WOORDEN.search(eigen))


def agenda_check(cfg: Config, agenda, verzoek: dict) -> dict:
    """Wat de app in je agenda zag bij de voorgestelde momenten, als blok voor Claude, plus een telling voor het
    paneel. Zonder titels van afspraken: Claude hoort alleen vrij of bezet."""
    from datetime import date as _date
    morgen = _date.today() + timedelta(days=1)

    def dag(x, standaard):
        try:
            return _date.fromisoformat(str(x)[:10]) if x else standaard
        except ValueError:
            return standaard
    vanaf = max(morgen, dag(verzoek.get("vroegst"), morgen))
    uiterlijk = dag(verzoek.get("uiterlijk"), vanaf + timedelta(days=14))
    if uiterlijk < vanaf:
        uiterlijk = vanaf + timedelta(days=7)
    duur = max(15, min(480, int(verzoek.get("duur_minuten") or 30)))
    m = afspraak_momenten(cfg, agenda, verzoek, vanaf, uiterlijk, "", duur)
    noem = lambda d: f"{d:%A} {d.day} {d:%B %Y}"
    regels, bezet = [], 0
    for p in m["voorgesteld"]:
        d = _date.fromisoformat(p["dag"])
        if p["verleden"]:
            stand = "ligt in het verleden"
        elif p["te_vroeg"]:
            stand = "vandaag, te kort dag"
        elif p["vrij"]:
            stand = f"vrij, bijvoorbeeld om {p['vrij'][11:16]}"
        else:
            stand, bezet = "bezet", bezet + 1
        regels.append(f"- {p['omschrijving']} ({noem(d)}): {stand}")
    vrij = [datetime.fromisoformat(x) for x in m["vrij"]]
    blok = ["<agenda_check>", "De app heeft de agenda van de eigenaar nagekeken (werktijd en bestaande afspraken)."]
    if regels:
        blok += ["Momenten uit de mail:"] + regels
    blok.append(f"Vrije momenten van {duur} minuten: " + ", ".join(f"{noem(v)} om {v:%H:%M}" for v in vrij)
                if vrij else "Geen vrije momenten gevonden in de komende twee weken.")
    blok.append("</agenda_check>")
    return {"blok": "\n".join(blok), "telling": {"bezet": bezet, "vrij": len(vrij), "voorgesteld": len(regels)}}


def afspraak_nakijken(cfg: Config, llm, agenda, mail: Email, tekst: str, actie: str, achtergrond: str | None):
    """Bij een mail over een afspraak: wat de afzender wil (Claude) en of je dan kunt (agenda).
    Geeft (verzoek, check) of (None, None). Een fout hier houdt het concept niet tegen; Claude zegt dan niets toe."""
    if not lijkt_afspraak(actie, mail.tekst) or not hasattr(llm, "afspraak_uit_mail"):
        return None, None
    ag = agenda() if callable(agenda) else agenda
    if ag is None:
        return None, None
    try:
        verzoek = llm.afspraak_uit_mail(mail, tekst, achtergrond)
        return verzoek, agenda_check(cfg, ag, verzoek)
    except Exception as e:
        log.warning("agenda nakijken voor %s mislukt: %s", mail.sleutel, e)
        return None, None


def afzender_adres(van: str) -> str:
    return parseaddr(van)[1].lower() or van.strip().lower()


def map_voorstellen(store: Store, item, mappen: list[str], aantal: int = 3) -> list[dict]:
    """Waarschijnlijke mappen voor een mail: eerst waar je deze afzender eerder opborg, dan dit project,
    dan mappen waarvan de naam in het onderwerp voorkomt (ENTER27 -> 'ENTER÷IFITT')."""
    import re
    adres = afzender_adres(item["van"] or "")
    score: dict[str, float] = {}
    reden: dict[str, str] = {}
    for r in store.map_keuzes(item["bron"]):
        if r["map"] not in mappen:
            continue
        if r["adres"] == adres:
            score[r["map"]] = score.get(r["map"], 0) + 10 * r["aantal"]
            reden[r["map"]] = f"used {r['aantal']}× before for this sender"
        elif r["project"] and r["project"] == item["project"]:
            score[r["map"]] = score.get(r["map"], 0) + r["aantal"]
            reden.setdefault(r["map"], f"often used for {item['project']}")
    woorden = {w for w in re.findall(r"[a-z0-9]+", (item["onderwerp"] or "").lower()) if len(w) >= 4}
    for m in mappen:
        mapwoorden = {w for w in re.findall(r"[a-z0-9]+", m.split("/")[-1].lower()) if len(w) >= 4}
        if any(w.startswith(mw) or mw.startswith(w) for w in woorden for mw in mapwoorden):
            score[m] = score.get(m, 0) + 5
            reden.setdefault(m, "name matches the subject")
    project = (item["project"] or "").lower()
    for m in mappen:
        if project and project != "overig" and m.split("/")[-1].strip().lower() == project:
            score[m] = score.get(m, 0) + 3
            reden.setdefault(m, "same name as the project")
    beste = sorted(score, key=lambda m: -score[m])[:aantal]
    return [{"map": m, "reden": reden[m]} for m in beste]


def verplaats_items(store: Store, bronnen: dict, item_ids: list[int], doel: str) -> str:
    """Archiveren of naar de prullenbak, na een klik van de gebruiker. Niets wordt definitief verwijderd."""
    if doel not in ("archief", "prullenbak") and not (doel.startswith("map:") and len(doel) > 4):
        raise ValueError(f"onbekend doel: {doel}")
    per_bron: dict[str, list] = {}
    for i in item_ids:
        item = store.item(i)
        if item is None or item["status"] in ("gearchiveerd", "verwijderd", "opgeborgen"):
            continue
        per_bron.setdefault(item["bron"], []).append(item)

    gelukt, mislukt = 0, []
    nieuwe_status = {"archief": "gearchiveerd", "prullenbak": "verwijderd"}.get(doel, "opgeborgen")
    for bron_naam, items in per_bron.items():
        bron = bronnen.get(bron_naam)
        if bron is None or not hasattr(bron, "verplaats"):
            mislukt += [f"{it['onderwerp'][:40]} (account {bron_naam} can't move email)" for it in items]
            continue
        try:
            res = bron.verplaats([it["bron_id"] for it in items], doel)
        except Exception as e:
            mislukt += [f"{it['onderwerp'][:40]} ({e})" for it in items]
            continue
        for it in items:
            fout = res.get(it["bron_id"], "no result")
            if fout is None:
                store.bewaar_vorige_status(it["id"], it["status"])
                store.zet_status(it["id"], nieuwe_status, opmerking=doel[4:] if doel.startswith("map:") else None)
                if doel.startswith("map:"):
                    store.onthoud_map(bron_naam, doel[4:], afzender_adres(it["van"] or ""), it["project"])
                gelukt += 1
            else:
                mislukt.append(f"{it['onderwerp'][:40]} ({fout})")

    waar = ("the archive" if doel == "archief" else "the Bin (you can restore it in Apple Mail)"
            if doel in ("archief", "prullenbak") else f"folder {doel[4:]}")
    melding = f"{gelukt} " + ("email" if gelukt == 1 else "emails") + f" moved to {waar}"
    if mislukt:
        melding += f"; failed: {'; '.join(mislukt[:5])}"
    return melding


def markeer_mail(store: Store, bronnen: dict, item_id: int | None, aan: bool, kleur: int = 4) -> str | None:
    """Vlag op de mail achter een taak. Geeft een waarschuwing terug als het niet lukt; de taak gaat altijd door."""
    if not item_id:
        return None
    item = store.item(item_id)
    bron = bronnen.get(item["bron"]) if item else None
    if bron is None or not hasattr(bron, "markeer"):
        return None
    if not aan and store.open_taken_bij_mail(item_id) > 0:
        return None   # er staat nog een andere open taak bij deze mail
    try:
        bron.markeer(item["bron_id"], item["thread_id"], aan, kleur)
        store.log(item_id, "vlag gezet" if aan else "vlag weggehaald")
        return None
    except Exception as e:
        return f"couldn't {'set' if aan else 'remove'} the flag in Mail ({e})"


def maak_taak(store: Store, bronnen: dict, agenda, item_id: int, titel: str, stappen: list[str],
              minuten: int, deadline: str | None, start: datetime | None, archiveren: bool,
              vlag: int | None = 4) -> str:
    """Taak opslaan, eventueel inplannen in de agenda en de mail archiveren. Alleen na een klik."""
    item = store.item(item_id)
    if item is None:
        raise ValueError(f"Item {item_id} does not exist")
    meldingen = []
    gepland = None
    if start is not None:
        if agenda is None:
            raise ValueError("no calendar connected; create the task without scheduling it")
        notities = "\n".join(f"- {s}" for s in stappen) + f"\n\nRegarding: {item['onderwerp']} ({item['van']})"
        meldingen.append(agenda.maak_afspraak(afspraak_titel(titel, stappen), start, start + timedelta(minutes=minuten), notities))
        gepland = start.isoformat(timespec="minutes")
    taak_id = store.nieuwe_taak(item_id, item["project"], titel, deadline, minuten, stappen, gepland)
    if gepland:
        store.zet_afspraak_id(taak_id, getattr(agenda, "laatste_id", None))
    meldingen.insert(0, "Task created and linked to the email")
    if vlag is not None:   # eerst markeren, dan pas eventueel archiveren
        fout = markeer_mail(store, bronnen, item_id, True, vlag)
        meldingen.append(fout or "flagged as scheduled in Mail")
    if archiveren and item["status"] in ("ter_info", "genegeerd"):
        meldingen.append(verplaats_items(store, bronnen, [item_id], "archief"))
    elif item["status"] in ("ter_info", "genegeerd", "wacht", "fout", "handmatig"):
        # weg uit Beoordelen en Opruimen; het concept-antwoord blijft bewaard en staat klaar bij Beantwoorden
        store.zet_status(item_id, "taak", opmerking="turned into a task; the email stays in the inbox")
    return "; ".join(meldingen)


def plan_bestaande_taak(store: Store, agenda, taak_id: int, start: datetime, minuten: int) -> str:
    """Een open taak in de agenda zetten (vanuit Planning). Alleen na een klik."""
    if agenda is None:
        raise ValueError("no calendar connected")
    t = store.taak(taak_id)
    if t is None:
        raise ValueError("task not found")
    if t["gepland"]:
        raise ValueError("this task is already in your calendar")   # geen dubbele afspraken
    if t["klaar"]:
        raise ValueError("this task is already ticked off")
    stappen = json.loads(t["stappen"] or "[]")
    notities = "\n".join(f"- {s}" for s in stappen)
    if t["item_id"]:
        it = store.item(t["item_id"])
        if it is not None:
            notities += f"\n\nRegarding: {it['onderwerp']} ({it['van']})"
    melding = agenda.maak_afspraak(afspraak_titel(t["omschrijving"], stappen), start,
                                   start + timedelta(minutes=minuten), notities.strip())
    store.plan_taak(taak_id, start.isoformat(timespec="minutes"), minuten)
    store.zet_afspraak_id(taak_id, getattr(agenda, "laatste_id", None))
    return melding


def verzet_taak(store: Store, agenda, taak_id: int, start: datetime, minuten: int) -> str:
    """Ingeplande taak naar een ander moment of een andere duur (slepen in Planning). Overlappen mag."""
    if agenda is None:
        raise ValueError("no calendar connected")
    t = store.taak(taak_id)
    if t is None or not t["gepland"]:
        raise ValueError("this task isn't in your calendar")
    oud = datetime.fromisoformat(t["gepland"])
    stappen = json.loads(t["stappen"] or "[]")
    afspraak = t["afspraak_id"]
    if not afspraak and hasattr(agenda, "zoek_afspraak"):
        afspraak = agenda.zoek_afspraak(oud, t["omschrijving"])
    eind = start + timedelta(minutes=minuten)
    if afspraak and hasattr(agenda, "verplaats_afspraak"):
        melding = agenda.verplaats_afspraak(afspraak, oud, start, eind)
    else:   # de afspraak is weg uit de agenda: een nieuwe maken
        melding = agenda.maak_afspraak(afspraak_titel(t["omschrijving"], stappen), start, eind, "\n".join(f"- {s}" for s in stappen))
        afspraak = getattr(agenda, "laatste_id", None)
    store.plan_taak(taak_id, start.isoformat(timespec="minutes"), minuten)
    store.zet_afspraak_id(taak_id, afspraak)
    return melding


def haal_uit_agenda(store: Store, agenda, taak_id: int) -> str:
    """Ingeplande taak weer uit je agenda: de afspraak weg, de taak terug bij 'Nog in te plannen'. Na een klik."""
    if agenda is None:
        raise ValueError("no calendar connected")
    t = store.taak(taak_id)
    if t is None or not t["gepland"]:
        raise ValueError("this task isn't in your calendar")
    afspraak = t["afspraak_id"]
    if not afspraak and hasattr(agenda, "zoek_afspraak"):   # taken van vóór deze versie: zoeken op tijd en titel
        afspraak = agenda.zoek_afspraak(datetime.fromisoformat(t["gepland"]), t["omschrijving"])
    weg = bool(afspraak) and hasattr(agenda, "verwijder_afspraak") and agenda.verwijder_afspraak(afspraak)
    store.ontplan_taak(taak_id)
    return ("Removed from your calendar; the task is back under To schedule" if weg else
            "The event was no longer in your calendar; the task is back under To schedule")


def open_antwoord(store: Store, bronnen: dict, item_id: int, tekst: str) -> str:
    """Antwoordvenster openen voor de mail achter een taak. Verandert niets aan taak of status."""
    item = store.item(item_id)
    if item is None:
        raise ValueError("email not found")
    bron = bronnen.get(item["bron"])
    if bron is None:
        raise ValueError(f"Account '{item['bron']}' is no longer in the configuration")
    store.zet_concept(item_id, tekst)
    if tekst.strip():
        store.zet_eigen_antwoord(item_id, tekst)
    melding = bron.maak_concept(item["bron_id"], tekst, item["onderwerp"], item["van"], message_id=item["thread_id"])
    store.log(item_id, "antwoord geopend vanuit taak")
    return melding


def zet_terug(store: Store, bronnen: dict, item_ids: list[int]) -> str:
    """Ongedaan maken van archiveren, opbergen of verwijderen: mail terug naar de inbox, item terug in de lijst."""
    gelukt, mislukt = 0, []
    for i in item_ids:
        it = store.item(i)
        if it is None or it["status"] not in ("gearchiveerd", "verwijderd", "opgeborgen"):
            continue
        van = {"gearchiveerd": "archief", "verwijderd": "prullenbak"}.get(it["status"]) or f"map:{it['opmerking']}"
        bron = bronnen.get(it["bron"])
        if bron is None or not hasattr(bron, "terugzetten") or not it["thread_id"]:
            mislukt.append(it["onderwerp"][:40])
            continue
        try:
            res = bron.terugzetten([it["thread_id"]], van)
        except Exception as e:
            mislukt.append(f"{it['onderwerp'][:40]} ({e})")
            continue
        nieuw = res.get(it["thread_id"], "FOUT:geen antwoord")
        if nieuw.startswith("FOUT"):
            mislukt.append(f"{it['onderwerp'][:40]} ({nieuw[5:]})")
            continue
        if nieuw != it["bron_id"]:
            store.nieuw_bron_id(it["sleutel"], it["bron"], nieuw)
        store.zet_status(i, it["vorige_status"] or "ter_info")
        store.db.execute("UPDATE items SET opmerking=NULL WHERE id=?", (i,))   # mapnaam e.d. niet als melding tonen
        store.db.commit()
        gelukt += 1
    melding = f"{gelukt} " + ("email" if gelukt == 1 else "emails") + " back in your inbox"
    if mislukt:
        melding += f"; failed: {'; '.join(mislukt[:3])}"
    return melding


def afspraak_titel(titel: str, stappen: list[str]) -> str:
    """Titel met de eerste concrete stap erin (als-dan-plan: om 14:00 begin ik met X)."""
    eerste = next((s.strip() for s in stappen if s.strip()), "")
    if not eerste:
        return titel
    return f"{titel} · start with: {eerste[0].lower() + eerste[1:]}"


class RondeBezig(RuntimeError):
    """Er loopt al een mailronde (gepland of vanuit de app)."""


WEGGEZET = ("gearchiveerd", "opgeborgen", "verwijderd")


def herken_mail(store: Store, bron: str, bron_id: str, message_id: str | None) -> bool:
    """Is deze mail al verwerkt? Eerst op het nummer van Apple Mail, dan op het Message-ID. Dat nummer verandert
    als een mail verplaatst wordt of Mail de inbox opnieuw opbouwt; het Message-ID nooit (fout 5).

    Een bekende mail met een nieuw nummer krijgt dat nummer, zonder Claude. Had attaché hem weggezet (archief,
    een map, de prullenbak, of na je antwoord gearchiveerd) en staat hij nu weer in je inbox, dan heb je hem
    zelf teruggezet: het item komt terug in To do of Tidy up. Stond hij al die tijd in je inbox (Mail bouwde
    hem opnieuw op), dan verandert er niets."""
    if store.is_bekend_sleutel(f"{bron}:{bron_id}"):
        return True
    it = store.item_bij_message_id(bron, message_id) if message_id else None
    if it is None:
        return False
    store.nieuw_bron_id(it["sleutel"], bron, bron_id)
    if it["status"] in WEGGEZET or it["opgeruimd"]:
        if store.open_taken_bij_mail(it["id"]) > 0:
            store.log(it["id"], "zelf teruggezet in de inbox; staat al in Planning bij een taak")
        else:
            vorige = it["vorige_status"] if it["status"] in WEGGEZET else None
            status = vorige if vorige in OPEN_STATUSSEN else "wacht"
            store.zet_terug_in_inbox(it["id"], status, wis_concept=bool(it["eigen_antwoord"]) and status == "wacht")
    else:
        store.log(it["id"], f"nieuw nummer in Mail ({bron_id}), zelfde mail")
    return True


@contextmanager
def ronde_slot(store: Store):
    """Eén mailronde tegelijk, ook tussen de geplande ronde en de knop Mail ophalen."""
    pad = Path(store.pad)
    slot = pad.with_name(pad.name + ".ronde.lock") if store.pad != ":memory:" else None
    if slot is None:
        yield
        return
    with open(slot, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RondeBezig("A mail round is already running. Try again in a few minutes.") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run(cfg: Config, store: Store, bronnen: dict, llm: LLM, dagen: int | None = None,
        maximum: int | None = None, voortgang: dict | None = None, agenda=None) -> dict[str, int]:
    """Verwerkt per bron de mail van de afgelopen `terugkijken_dagen` die nog niet verwerkt is, oudste eerst.

    Elke ronde neemt hoogstens `maximum` mails per bron; de volgende ronde gaat verder.
    Al verwerkte mail wordt herkend en niet opnieuw naar Claude gestuurd.
    `voortgang` krijgt per bron het aantal mails dat daarna nog te gaan is.
    """
    if agenda is None:   # pas koppelen als een mail over een afspraak gaat
        cache: dict = {}

        def agenda():
            if "ag" not in cache:
                from .agenda import maak_agenda
                cache["ag"] = maak_agenda(cfg)[0] if (cfg.agenda or {}).get("kalender") else None
            return cache["ag"]
    with ronde_slot(store):
        resultaat: dict[str, int] = {}
        uitgaand: dict[str, list] = {}   # per bron: je verstuurde mail van de laatste 30 dagen
        maximum = maximum or cfg.max_mails_per_run
        nu = datetime.now().astimezone()
        sinds = nu - timedelta(days=dagen or cfg.terugkijken_dagen)
        for naam, bron in bronnen.items():
            log.info("%s: mail van de afgelopen %d dagen doorzoeken…", naam, dagen or cfg.terugkijken_dagen)
            try:
                mails = bron.haal_op(sinds, maximum,
                                     overslaan=lambda bid, mid=None, n=naam: herken_mail(store, n, bid, mid))
            except Exception as e:  # een kapotte bron mag de andere niet tegenhouden
                log.error("Ophalen uit %s mislukt: %s", naam, e)
                store.log(None, f"ophalen {naam} mislukt: {e}")
                resultaat[naam] = -1
                continue

            if hasattr(bron, "verzonden_sinds"):   # eerst: als achtergrond bij de nieuwe concepten
                try:
                    uitgaand[naam] = bron.verzonden_sinds(nu - timedelta(days=30))
                    bewaar_verstuurd(store, naam, uitgaand[naam])
                except Exception as e:
                    log.warning("%s: verzonden mail niet te lezen: %s", naam, e)
            totaal = getattr(bron, "laatste_totaal", len(mails))
            te_doen = [m for m in sorted(mails, key=lambda m: (not is_belangrijk(cfg, store, m.van), m.datum))
                       if not store.is_bekend(m)]   # belangrijke afzenders eerst
            log.info("%s: %d onverwerkte mail(s) sinds %s, deze ronde %d", naam, totaal, f"{sinds:%d-%m}", len(te_doen))
            nieuw = 0
            for i, mail in enumerate(te_doen, 1):
                log.info("%s %d/%d: %s", naam, i, len(te_doen), mail.onderwerp[:70])
                try:
                    item_id = verwerk_mail(cfg, store, llm, mail, agenda)
                except Exception as e:  # deze mail is niet opgeslagen; de volgende ronde probeert hem opnieuw
                    log.error("%s: %s overgeslagen door een fout, volgende ronde opnieuw: %s", naam, mail.sleutel, e)
                    continue
                item = store.item(item_id)
                log.info("   -> %s, %s (%s)", item["project"], item["actie"], item["status"])
                nieuw += 1
            if mails:
                store.zet_laatst_opgehaald(naam, max(m.datum for m in mails))
            rest = max(0, totaal - len(mails))
            if voortgang is not None:
                voortgang[naam] = rest
            if rest:
                log.info("%s: nog %d mail(s) te gaan; de volgende ronde gaat verder", naam, rest)
            store.log(None, f"run {naam}: {len(mails)} van {totaal} verwerkt, nog {rest}")
            resultaat[naam] = nieuw
        try:
            telling = controleer_uitgaand(cfg, store, bronnen, llm, uitgaand=uitgaand)
            log.info("controle: %(taken)d taken en %(beantwoord)d mails lijken al gedaan; wacht op antwoord: "
                     "%(wacht_nieuw)d nieuw, %(wacht_opgelost)d beantwoord", telling)
        except Exception as e:  # de controle mag de ronde niet laten mislukken
            log.warning("controle na de ronde mislukt: %s", e)
        try:
            n = ruim_beantwoord_op(cfg, store, bronnen)
            if n:
                log.info("%d beantwoorde mail(s) gearchiveerd", n)
        except Exception as e:
            log.warning("opruimen na de ronde mislukt: %s", e)
        return resultaat


def keur_goed(store: Store, bronnen: dict, item_id: int, concept: str | None,
              taken: list[dict] | None = None, verzenden: bool = False) -> str:
    """Goedkeuren: concept in Mail zetten (of na jouw bevestigde klik meteen versturen) en taken opslaan.
    Versturen gebeurt alleen vanuit de app na een klik van de gebruiker, nooit tijdens een ronde."""
    item = store.item(item_id)
    if item is None:
        raise ValueError(f"Item {item_id} does not exist")
    if item["status"] not in ("wacht", "fout", "handmatig"):
        raise ValueError(f"Item {item_id} has already been handled ({item['status']})")

    meldingen = []
    concept = (concept or "").strip() or None
    if verzenden and not concept:
        raise ValueError("there is no text to send")
    if verzenden and OPEN_PLEK.search(concept):
        raise ValueError("the draft still contains [FILL IN: …]; fill that in first")
    verzonden = False
    if concept:
        bron = bronnen.get(item["bron"])
        if bron is None:
            raise ValueError(f"Account '{item['bron']}' is no longer in the configuration")
        store.zet_concept(item_id, concept)   # jouw tekst blijft bewaard, ook als Mail hierna faalt
        if verzenden:
            uitkomst = bron.maak_concept(item["bron_id"], concept, item["onderwerp"], item["van"],
                                         message_id=item["thread_id"], verzenden=True)
            verzonden = getattr(bron, "laatste_uitkomst", None) == "verzonden"
            if not verzonden:
                # niet aantoonbaar verstuurd: de mail blijft in Te doen, met jouw tekst bewaard
                store.log(item_id, f"verzenden niet bevestigd: {uitkomst}")
                return uitkomst
            meldingen.append(f"sent to {item['van']}")
        else:
            meldingen.append(bron.maak_concept(item["bron_id"], concept, item["onderwerp"], item["van"],
                                               message_id=item["thread_id"]))
    if taken is None:   # niet aangepast: de taken zoals Claude ze voorstelde
        taken = json.loads(item["taken"] or "[]")
    taken = [t for t in schone_taken(taken) if not t["antwoord"]]   # je antwoordt nu, dus die taak vervalt
    if taken:
        store.voeg_taken_toe(item_id, item["project"], taken)
        meldingen.append("1 task added" if len(taken) == 1 else f"{len(taken)} tasks added")
    melding = "; ".join(meldingen) or "approved without a draft or tasks"
    store.zet_status(item_id, "verzonden" if verzonden else "goedgekeurd", concept=concept, opmerking=melding)
    if concept:
        store.zet_eigen_antwoord(item_id, concept)
    store.log(item_id, "VERZONDEN na bevestigde klik" if verzonden else "concept in Mail gezet")
    return melding


def ruim_beantwoord_op(cfg: Config, store: Store, bronnen: dict, item_ids: list[int] | None = None,
                       slot=None) -> int:
    """Mail die je via de app beantwoordde naar het archief, zodra je antwoord echt verstuurd is: na Send meteen,
    na Reply in Mail zodra Mail het origineel als beantwoord markeert (dat doet Mail zelf bij versturen).
    Een concept dat je weggooit, laat de mail dus in je inbox. Archiveren zet hem ook op gelezen.
    Mail die al niet meer in je inbox staat, omdat je hem zelf verplaatste, laat de app met rust."""
    from collections import defaultdict
    from contextlib import nullcontext

    if not cfg.opruimen_na_antwoord:
        return 0
    rijen = store.op_te_ruimen(cfg.terugkijken_dagen)
    if item_ids is not None:
        rijen = [r for r in rijen if r["id"] in set(item_ids)]
    per_bron = defaultdict(list)
    for r in rijen:
        per_bron[r["bron"]].append(r)
    slot = slot or nullcontext()
    gelukt = 0
    for bron_naam, items in per_bron.items():
        bron = bronnen.get(bron_naam)
        if bron is None or not hasattr(bron, "verplaats") or not hasattr(bron, "beantwoord"):
            continue
        try:
            with slot:
                stand = bron.beantwoord([(it["bron_id"], it["thread_id"]) for it in items])
        except Exception as e:
            log.warning("%s: niet na te gaan welke mail beantwoord is: %s", bron_naam, e)
            continue
        klaar = []
        for it in items:
            wat, huidig = stand.get(it["bron_id"], ("nee", None))
            if wat == "weg":
                store.zet_opgeruimd(it["id"], "weg")
            elif it["status"] == "verzonden" or wat == "ja":
                klaar.append((it, huidig or it["bron_id"]))
        if not klaar:
            continue
        try:
            with slot:
                res = bron.verplaats([bid for _, bid in klaar], "archief")
        except Exception as e:
            log.warning("%s: archiveren na je antwoord mislukt: %s", bron_naam, e)
            continue
        for it, bid in klaar:
            fout = res.get(bid, "no result")
            if fout is None:
                store.zet_opgeruimd(it["id"], datetime.now().astimezone().isoformat(timespec="seconds"))
                store.log(it["id"], "na je antwoord gearchiveerd en op gelezen gezet")
                gelukt += 1
            else:
                log.warning("%s: archiveren na je antwoord mislukt voor item %s: %s", bron_naam, it["id"], fout)
    return gelukt


VOORVOEGSEL = re.compile(r"^\s*((re|aw|antw|fw|fwd|wg|doorst)\s*:\s*)+", re.IGNORECASE)
DOORSTUREN = re.compile(r"^\s*(fw|fwd|wg|doorst)\s*:", re.IGNORECASE)
CITAAT = re.compile(r"^\s*(>|Op .+ schreef|On .+ wrote|-{3,}\s*Original Message|Van:\s|From:\s|_{8,})", re.IGNORECASE | re.MULTILINE)


def _kaal_onderwerp(onderwerp: str) -> str:
    return VOORVOEGSEL.sub("", onderwerp or "").strip().lower()


def _eigen_tekst(tekst: str) -> str:
    """Alleen wat je zelf schreef: alles vanaf het geciteerde origineel valt weg."""
    m = CITAAT.search(tekst or "")
    return (tekst[:m.start()] if m else tekst or "").strip()[:3000]


def zoek_antwoorden(item, verstuurd: list[dict]) -> list[dict]:
    """Door jou verstuurde mail die een antwoord (of doorgestuurde versie) is op deze mail, oudste eerst.

    Zeker: het Message-ID van de mail staat in In-Reply-To of References. Anders: zelfde onderwerp
    (zonder Re:/Fw:) en verstuurd aan de afzender, of doorgestuurd."""
    mid = (item["thread_id"] or "").strip().strip("<>").lower()
    afzender = afzender_adres(item["van"] or "")
    onderwerp = _kaal_onderwerp(item["onderwerp"])
    na = datetime.fromisoformat(item["datum"])
    treffers = []
    for v in verstuurd:
        if v["datum"] <= na:
            continue
        verwijst = f"{v['in_reply_to']} {v['references']}".lower()
        zeker = bool(mid) and mid in verwijst
        zelfde = bool(onderwerp) and _kaal_onderwerp(v["onderwerp"]) == onderwerp and (
            afzender in v["aan"] or bool(DOORSTUREN.match(v["onderwerp"] or "")))
        if zeker or zelfde:
            treffers.append(v)
    return sorted(treffers, key=lambda v: v["datum"])


MID = re.compile(r"<([^<>\s]+)>")


def _ids(tekst: str | None) -> set[str]:
    """Message-ID's uit In-Reply-To/References (of één losse ID), zonder haakjes, in kleine letters."""
    tekst = tekst or ""
    gevonden = {m.lower() for m in MID.findall(tekst)}
    if not gevonden and tekst.strip() and " " not in tekst.strip():
        gevonden = {tekst.strip().strip("<>").lower()}
    return gevonden


def bewaar_verstuurd(store: Store, bron: str, verstuurd: list[dict]) -> None:
    """Je verstuurde mail onthouden als achtergrond voor latere antwoorden (alleen je eigen tekst)."""
    store.bewaar_verstuurd(bron, [{**v, "eigen": _eigen_tekst(v["tekst"])} for v in verstuurd])


def achtergrond(cfg: Config, store: Store, *, sleutel: str, van: str, onderwerp: str, thread_id: str | None,
                verwijzingen: str | None, datum: datetime, dagen: int = 120) -> tuple[str, list[dict]]:
    """Wat Claude moet weten om goed te antwoorden: eerdere mail in dit gesprek (ook jouw antwoorden vanaf
    een ander apparaat), eerdere gesprekken met dezelfde afzender, en wat er met die persoon nog openstaat.

    Alleen je eigen tekst en de nieuwe tekst van elke mail, zonder citaten, en niets van afzenders of
    ontvangers op de lijst 'nooit naar Claude'. Geeft (tekst voor Claude, lijst voor de app)."""
    adres = afzender_adres(van or "")
    kaal = _kaal_onderwerp(onderwerp)
    draad_ids = _ids(thread_id) | _ids(verwijzingen)
    grens = datum - timedelta(days=dagen)
    stukken = []   # (datum, in_draad, richting, wie, onderwerp, tekst)

    def mag(wie: str, ond: str) -> bool:
        return mag_naar_claude(cfg, Email(bron="", bron_id="", van=wie, aan="", onderwerp=ond or "", datum=datum, tekst=""))

    for m in store.mails_met_tekst():
        if m["sleutel"] == sleutel:
            continue
        try:
            d = datetime.fromisoformat(m["datum"])
        except (TypeError, ValueError):
            continue
        if d < grens or d > datum + timedelta(days=1):
            continue
        m_adres = afzender_adres(m["van"] or "")
        in_draad = bool(draad_ids & (_ids(m["thread_id"]) | _ids(m["verwijzingen"]))) or (
            bool(kaal) and _kaal_onderwerp(m["onderwerp"]) == kaal and m_adres == adres)
        if not (in_draad or (adres and m_adres == adres)) or not mag(m["van"] or "", m["onderwerp"]):
            continue
        stukken.append((d, in_draad, "in", m["van"] or m_adres, m["onderwerp"] or "", _eigen_tekst(m["tekst"] or "")))
    for v in store.verstuurd_lijst():
        try:
            d = datetime.fromisoformat(v["datum"])
        except (TypeError, ValueError):
            continue
        if d < grens or d > datum + timedelta(days=1):
            continue
        aan = (v["aan"] or "").lower()
        in_draad = bool(draad_ids & _ids(v["verwijzingen"])) or (bool(kaal) and _kaal_onderwerp(v["onderwerp"]) == kaal
                                                                  and adres in aan)
        if not (in_draad or (adres and adres in aan)) or not mag(aan, v["onderwerp"]):
            continue
        stukken.append((d, in_draad, "uit", aan.split(",")[0].strip(), v["onderwerp"] or "", v["tekst"] or ""))

    draad = sorted([x for x in stukken if x[1]], key=lambda x: x[0])[-8:]
    eerder = sorted([x for x in stukken if not x[1]], key=lambda x: x[0], reverse=True)[:6]
    regels, lijst, ruimte = [], [], 6000

    def blok(x, maat):
        nonlocal ruimte
        d, _, richting, wie, ond, tekst = x
        kop = f"{d.astimezone():%d-%m-%Y} · " + (f"jij aan {wie}" if richting == "uit" else f"van {wie}") + f" · {ond}"
        stuk = f"{kop}\n{tekst[:maat].strip()}"
        if len(stuk) > ruimte:
            return None
        ruimte -= len(stuk)
        lijst.append({"soort": "draad" if x[1] else "eerder", "richting": richting, "datum": d.isoformat(),
                      "wie": wie, "onderwerp": ond})
        return stuk

    if draad:
        regels.append("Eerder in dit gesprek (oud naar nieuw):")
        regels += [b for b in (blok(x, 900) for x in draad) if b]
    if eerder:
        regels.append("Andere recente mail met dezelfde persoon (nieuw naar oud):")
        regels += [b for b in (blok(x, 450) for x in eerder) if b]

    open_taken = []
    for t in store.open_taken():
        if t["item_id"]:
            it = store.item(t["item_id"])
            if it is not None and afzender_adres(it["van"] or "") == adres:
                open_taken.append(f"- {t['omschrijving']}" + (f" (deadline {t['deadline']})" if t["deadline"] else ""))
    if open_taken:
        regels.append("Open taken die al bij deze afzender horen:\n" + "\n".join(open_taken[:6]))
        lijst.append({"soort": "taken", "aantal": len(open_taken)})
    wacht = [w for w in store.wacht_op_lijst("wacht") if adres and adres in (w["aan"] or "").lower()]
    if wacht:
        regels.append("Je wacht nog op antwoord van deze persoon op:\n" + "\n".join(
            f"- {w['onderwerp']} (verstuurd {w['verstuurd'][:10]}){': ' + w['wat'] if w['wat'] else ''}" for w in wacht[:4]))
        lijst.append({"soort": "wacht", "aantal": len(wacht)})
    return anonimiseer(cfg, "\n\n".join(regels)), lijst


def achtergrond_voor_item(cfg: Config, store: Store, it) -> tuple[str, list[dict]]:
    return achtergrond(cfg, store, sleutel=it["sleutel"], van=it["van"] or "", onderwerp=it["onderwerp"] or "",
                       thread_id=it["thread_id"], verwijzingen=it["verwijzingen"],
                       datum=datetime.fromisoformat(it["datum"]))


def is_belangrijk(cfg: Config, store: Store, van: str) -> bool:
    """Staat de afzender op je lijst belangrijke afzenders (config of sterretje in de app)?"""
    adres = afzender_adres(van or "")
    if not adres:
        return False
    for e in list(cfg.belangrijk) + [r["adres"] for r in store.belangrijke()]:
        e = e.strip().lower()
        if not e:
            continue
        if "@" not in e:
            e = "@" + e                       # alleen een domein: istte.org
        if adres == e or (e.startswith("@") and adres.endswith(e)):
            return True
    return False


def plus_werkdagen(dag, n: int, werkdagen: set[int]) -> "date":
    d = dag.date() if isinstance(dag, datetime) else dag   # datetime is ook een date: eerst die
    while n > 0:
        d += timedelta(days=1)
        if d.isoweekday() in werkdagen:
            n -= 1
    return d


VRAAGT = re.compile(r"\?|\b(graag|kun je|kunt u|zou je|zou u|wil je|laat (je|u) .*weten|hoor (ik )?graag|"
                    r"could you|can you|would you|please|let me know|looking forward to)\b", re.IGNORECASE)


def _later_contact(item, verstuurd: list[dict], dagen: int = 14):
    """Laatste mail die je na deze mail aan dezelfde afzender stuurde (ander onderwerp), binnen `dagen`."""
    afzender = afzender_adres(item["van"] or "")
    na = datetime.fromisoformat(item["datum"])
    kandidaten = [v for v in verstuurd if afzender and afzender in v["aan"] and na < v["datum"] <= na + timedelta(days=dagen)]
    return max(kandidaten, key=lambda v: v["datum"]) if kandidaten else None


def controleer_taken(cfg: Config, store: Store, bronnen: dict, llm: LLM | None = None, slot=None) -> int:
    return controleer_uitgaand(cfg, store, bronnen, llm, slot)["taken"]


def controleer_uitgaand(cfg: Config, store: Store, bronnen: dict, llm: LLM | None = None, slot=None,
                        uitgaand: dict | None = None) -> dict:
    """Leest per bron je map Verzonden (van elk apparaat) en doet daar drie dingen mee.

    1. Taken bij een mail die je al beantwoordde krijgen 'lijkt gedaan' (andere taken: Claude kijkt mee).
    2. Mail in Te doen die je al beantwoordde krijgt 'lijkt beantwoord'.
    3. Nieuwe mail waarin jij iets vraagt, komt op de lijst 'wacht op antwoord'.
    Daarna sluit hij 'wacht op antwoord' af voor mail waarop een antwoord binnenkwam.
    De app vinkt en handelt niets zelf af. Geeft tellingen terug."""
    from collections import defaultdict
    from contextlib import nullcontext

    telling = {"taken": 0, "beantwoord": 0, "wacht_nieuw": 0, "wacht_opgelost": 0}
    nu_ = datetime.now().astimezone()
    per_item = defaultdict(list)
    for t in store.open_taken():
        if t["item_id"]:
            per_item[t["item_id"]].append(t)
    taak_items, te_doen = defaultdict(list), defaultdict(list)
    for item_id in per_item:
        it = store.item(item_id)
        if it is not None:
            taak_items[it["bron"]].append(it)
    for status in ("wacht", "fout", "handmatig"):
        for r in store.items(status=status):
            it = store.item(r["id"])
            te_doen[it["bron"]].append(it)

    for bron_naam, bron in bronnen.items():
        if not hasattr(bron, "verzonden_sinds"):
            continue
        items = taak_items[bron_naam] + te_doen[bron_naam]
        # de laatste week; wat al bekeken is, onthoudt de lijst (ook mail die later van je telefoon binnenkomt)
        nieuw_vanaf = nu_ - timedelta(days=7)
        sinds = min([datetime.fromisoformat(it["datum"]) for it in items] + [nieuw_vanaf])
        sinds = max(sinds, nu_ - timedelta(days=30))
        if uitgaand and bron_naam in uitgaand:          # al gelezen aan het begin van de ronde
            verstuurd = [v for v in uitgaand[bron_naam] if v["datum"] > sinds]
        else:
            try:
                with (slot or nullcontext()):
                    verstuurd = bron.verzonden_sinds(sinds)
            except Exception as e:
                log.warning("%s: verzonden mail niet te lezen: %s", bron_naam, e)
                continue
            bewaar_verstuurd(store, bron_naam, verstuurd)

        # 1. taken
        for it in taak_items[bron_naam]:
            telling["taken"] += _taken_met_antwoord(cfg, store, llm, it, per_item[it["id"]], verstuurd)
        # 2. Te doen
        for it in te_doen[bron_naam]:
            antwoorden = zoek_antwoorden(it, verstuurd)
            if antwoorden:
                v = antwoorden[-1]
                soort = "forwarded" if DOORSTUREN.match(v["onderwerp"] or "") else "answered"
                ontvanger = v["aan"].split(",")[0].strip() or "the sender"
                d = v["datum"].astimezone()
                reden = f"You {soort} this email on {d.day} {d:%b %H:%M} (to {ontvanger})."
            else:
                # geen antwoord in dit gesprek, maar wel daarna gemaild met deze persoon: vragen of dat het was
                v = _later_contact(it, verstuurd)
                if v is None:
                    continue
                d = v["datum"].astimezone()
                reden = (f"You emailed this person afterwards, on {d.day} {d:%b %H:%M}, with the subject "
                         f"\u201c{v['onderwerp'] or '(none)'}\u201d. Was that your reply?")
            if it["bewijs"] == v["message_id"]:
                continue          # al gemeld met dit bericht, of door jou op 'nog niet' gezet
            store.zet_lijkt_beantwoord(it["id"], reden, v["message_id"])
            telling["beantwoord"] += 1
        # 3. wacht op antwoord
        if cfg.opvolgen_aan:
            telling["wacht_nieuw"] += _nieuwe_wacht_op(cfg, store, llm, bron_naam,
                                                       [v for v in verstuurd if v["datum"] > nieuw_vanaf])

    telling["wacht_opgelost"] = _sluit_wacht_op(store)
    return telling


def _taken_met_antwoord(cfg: Config, store: Store, llm, it, taken: list, verstuurd: list[dict]) -> int:
    """Taak 'Beantwoorden: …' lijkt gedaan zodra er een antwoord is; andere taken legt de app aan Claude voor."""
    antwoorden = zoek_antwoorden(it, verstuurd)
    if not antwoorden:
        return 0
    laatste = antwoorden[-1]
    bewijs = laatste["message_id"]
    ontvanger = laatste["aan"].split(",")[0].strip() or "the sender"
    _d = laatste["datum"].astimezone()
    wanneer = f"{_d.day} {_d:%b %H:%M}"
    nieuw, te_vragen = 0, []
    for t in taken:
        if t["bewijs"] == bewijs:
            continue          # al bekeken met dit antwoord (of door jou op 'nog niet' gezet)
        if t["soort"] == "antwoord":
            soort = "forwarded" if DOORSTUREN.match(laatste["onderwerp"] or "") else "answered"
            store.zet_lijkt_klaar(t["id"], f"You {soort} this email on {wanneer} (to {ontvanger})", bewijs)
            nieuw += 1
        else:
            te_vragen.append(t)
    mail = Email(bron=it["bron"], bron_id=it["bron_id"], van=it["van"] or "", aan=it["aan"] or "",
                 onderwerp=it["onderwerp"] or "", datum=datetime.fromisoformat(it["datum"]), tekst="")
    if not te_vragen or llm is None or not hasattr(llm, "taken_gedaan") or not mag_naar_claude(cfg, mail):
        return nieuw
    eigen = "\n\n---\n\n".join(_eigen_tekst(v["tekst"]) for v in antwoorden)
    try:
        oordeel = llm.taken_gedaan(it["onderwerp"] or "", [(t["id"], t["omschrijving"]) for t in te_vragen],
                                   anonimiseer(cfg, eigen))
    except Exception as e:
        log.warning("taakcontrole voor %s mislukt: %s", it["sleutel"], e)
        return nieuw
    klaar = {o.taak_id: o.reden for o in oordeel.taken if o.klaar}
    for t in te_vragen:
        if t["id"] in klaar:
            store.zet_lijkt_klaar(t["id"], f"Your email of {wanneer} to {ontvanger}: {klaar[t['id']]}", bewijs)
            nieuw += 1
        else:
            store.zet_lijkt_klaar(t["id"], None, bewijs)   # bekeken, niet gedaan
    return nieuw


def _nieuwe_wacht_op(cfg: Config, store: Store, llm, bron_naam: str, verstuurd: list[dict]) -> int:
    """Verstuurde mail waarin je iets vraagt: op de lijst, terug in Te doen na x werkdagen zonder antwoord."""
    from .agenda import lees_werktijd
    kandidaten = []
    for v in verstuurd:
        mid = v["message_id"]
        if mid and store.wacht_op_bekend(mid):
            store.vul_wacht_tekst(mid, _eigen_tekst(v["tekst"]))   # oudere regels kregen nog geen tekst
            continue
        if not mid or not v["aan"].strip(" ,"):
            continue
        if all(re.search(r"no-?reply|donotreply|niet-?beantwoorden", a) for a in v["aan"].split(",") if a.strip()):
            continue          # op een no-reply-adres komt geen antwoord
        eigen = _eigen_tekst(v["tekst"])
        ontvangers = Email(bron="", bron_id="", van=v["aan"], aan="", onderwerp=v["onderwerp"] or "",
                           datum=v["datum"], tekst="")
        if not VRAAGT.search(eigen) or not mag_naar_claude(cfg, ontvangers):
            store.nieuw_wacht_op(bron_naam, mid, v["aan"], v["onderwerp"], v["datum"].isoformat(), None,
                                 v["datum"].date().isoformat(), status="geen")   # onthouden: niet opnieuw bekijken
            continue
        kandidaten.append((v, eigen))
    if not kandidaten or llm is None or not hasattr(llm, "verwacht_antwoord"):
        return 0
    werkdagen = lees_werktijd(cfg.agenda).dagen
    nieuw = 0
    for begin in range(0, len(kandidaten), 10):            # tien per vraag aan Claude
        groep = kandidaten[begin:begin + 10]
        try:
            oordeel = llm.verwacht_antwoord([{"nr": i, "aan": v["aan"], "onderwerp": v["onderwerp"],
                                              "tekst": anonimiseer(cfg, eigen)} for i, (v, eigen) in enumerate(groep)])
        except Exception as e:
            log.warning("wacht op antwoord: Claude-fout: %s", e)
            return nieuw
        per_nr = {o.nr: o for o in oordeel.mails}
        for i, (v, _) in enumerate(groep):
            o = per_nr.get(i)
            verwacht = bool(o and o.verwacht_antwoord)
            terug = plus_werkdagen(v["datum"].astimezone(), cfg.opvolgen_werkdagen, werkdagen)
            aan = ", ".join(a.strip() for a in v["aan"].split(",") if a.strip())
            store.nieuw_wacht_op(bron_naam, v["message_id"], aan, v["onderwerp"], v["datum"].isoformat(),
                                 (o.wat if o else None), terug.isoformat(), status="wacht" if verwacht else "geen",
                                 tekst=_eigen_tekst(v["tekst"]))
            nieuw += verwacht
    return nieuw


def _sluit_wacht_op(store: Store) -> int:
    """Kwam er een antwoord binnen? Zeker via In-Reply-To/References, anders zelfde onderwerp van een ontvanger."""
    opgelost = 0
    for w in store.wacht_op_lijst("wacht"):
        verstuurd = datetime.fromisoformat(w["verstuurd"])
        mid = (w["message_id"] or "").strip().strip("<>").lower()
        onderwerp = _kaal_onderwerp(w["onderwerp"])
        ontvangers = {a.strip() for a in (w["aan"] or "").split(",") if a.strip()}
        for m in store.inkomend_sinds((verstuurd - timedelta(days=1)).isoformat()):
            try:
                if datetime.fromisoformat(m["datum"]) <= verstuurd:
                    continue
            except ValueError:
                continue
            zeker = bool(mid) and mid in (m["verwijzingen"] or "").lower()
            zelfde = bool(onderwerp) and _kaal_onderwerp(m["onderwerp"]) == onderwerp and afzender_adres(m["van"] or "") in ontvangers
            if zeker or zelfde:
                store.zet_wacht_op(w["id"], status="beantwoord", antwoord=m["sleutel"])
                opgelost += 1
                break
    return opgelost


def voorstellen_voor(agenda, cfg: Config, taken: list, vanaf: datetime) -> dict[int, str]:
    """Per taak het eerste vrije moment (vóór de deadline als die er is), zonder dat voorstellen botsen."""
    from datetime import date as _date
    from .agenda import lees_werktijd, vrije_momenten
    werk = lees_werktijd(cfg.agenda)
    bezet = agenda.bezet(vanaf, vanaf + timedelta(days=werk.dagen_vooruit + 1))
    gepland: list = []
    uit = {}
    for t in sorted(taken, key=lambda t: (t["deadline"] or "9999", t["id"])):
        minuten = t["minuten"] or 30
        try:
            dl = _date.fromisoformat(t["deadline"]) if t["deadline"] else None
        except ValueError:
            dl = None
        mom = vrije_momenten(bezet + gepland, minuten, vanaf, werk, dl, aantal=1)
        if mom:
            uit[t["id"]] = mom[0].isoformat(timespec="minutes")
            gepland.append((mom[0], mom[0] + timedelta(minutes=minuten)))
    return uit


def afspraak_momenten(cfg: Config, agenda, verzoek: dict, vanaf, uiterlijk, dagdeel: str, duur: int) -> dict:
    """Bij een verzoek om een afspraak: of jij kunt op de momenten uit de mail, en vrije momenten binnen jouw
    voorwaarden (niet vóór, uiterlijk, ochtend of middag, duur). Hoogstens één voorstel per dag."""
    from datetime import date as _date, time as _time
    from .agenda import Werktijd, _volgend_kwartier, lees_werktijd, vrije_momenten
    werk = lees_werktijd(cfg.agenda)
    vandaag = _date.today()

    def dag_van(x):
        try:
            return _date.fromisoformat(str(x)[:10])
        except ValueError:
            return None

    def tijd_van(x, standaard):
        try:
            return _time.fromisoformat(str(x)[:5]) if x else standaard
        except ValueError:
            return standaard
    voorstellen = [(p, dag_van(p.get("dag"))) for p in verzoek.get("voorgesteld") or []]
    voorstellen = [(p, d) for p, d in voorstellen if d]
    begin = min([vanaf] + [d for _, d in voorstellen])
    eind = max([uiterlijk] + [d for _, d in voorstellen])
    van_dt = datetime.combine(begin, _time(0)).astimezone()
    tot_dt = datetime.combine(eind + timedelta(days=1), _time(0)).astimezone()
    if hasattr(agenda, "afspraken"):
        alle = [a for a in agenda.afspraken(van_dt, tot_dt) if not a["heledag"] and not a.get("vrij")]
    else:
        alle = [{"titel": "Bezet", "start": b, "eind": e} for b, e in agenda.bezet(van_dt, tot_dt)]
    bezet = [(a["start"], a["eind"]) for a in alle]
    lengte = timedelta(minutes=duur)
    uit_mail = []
    for p, d in voorstellen:
        start = datetime.combine(d, tijd_van(p.get("van"), werk.begin)).astimezone()
        einde = datetime.combine(d, tijd_van(p.get("tot"), werk.eind)).astimezone()
        gevonden, t = None, _volgend_kwartier(max(start, datetime.now().astimezone()))
        while d >= vandaag and t + lengte <= einde:
            botsing = next((e for b, e in sorted(bezet) if b < t + lengte and e > t), None)
            if botsing is None:
                gevonden = t
                break
            t = _volgend_kwartier(max(botsing, t + timedelta(minutes=15)))
        botsingen = [a["titel"] for a in alle if a["start"] < einde and a["eind"] > start][:3]
        uit_mail.append({"omschrijving": p.get("omschrijving") or d.isoformat(), "dag": d.isoformat(),
                         "vrij": gevonden.isoformat(timespec="minutes") if gevonden and d >= vanaf else None,
                         "botsingen": botsingen, "verleden": d < vandaag, "te_vroeg": vandaag <= d < vanaf})
    w = Werktijd(dagen=werk.dagen, begin=werk.begin, eind=werk.eind,
                 dagen_vooruit=max(1, (uiterlijk - vanaf).days + 1))
    if dagdeel == "ochtend":
        w.eind = min(werk.eind, _time(12, 0))
    elif dagdeel == "middag":
        w.begin = max(werk.begin, _time(13, 0))
    nu_ = max(datetime.now().astimezone(), datetime.combine(vanaf, _time(0)).astimezone())
    vrij = vrije_momenten(bezet, duur, nu_, w, uiterlijk, aantal=4)
    return {"voorgesteld": uit_mail, "vrij": [m.isoformat(timespec="minutes") for m in vrij]}


def houd_vast(store: Store, agenda, item_id: int, momenten: list[str], duur: int, onderwerp: str, met: str) -> str:
    """Aangeboden momenten voorlopig in je agenda, zodat er niets anders in komt. Na een klik."""
    if agenda is None or not hasattr(agenda, "maak_voorlopig"):
        raise ValueError("this calendar can't hold tentative events")
    item = store.item(item_id)
    titel = onderwerp + (f" ({met})" if met else "")
    for m in momenten:
        start = datetime.fromisoformat(m).astimezone()
        eind = start + timedelta(minutes=duur)
        aid = agenda.maak_voorlopig(f"Tentative: {titel}", start, eind,
                                    f"Proposed to {met or 'the sender'}, regarding: {item['onderwerp'] if item else ''}")
        store.bewaar_voorlopig(aid, item_id, titel, start.isoformat(timespec="minutes"), eind.isoformat(timespec="minutes"))
    return (f"{len(momenten)} times held as tentative" if len(momenten) != 1 else "1 time held as tentative")


def bevestig_voorlopig(store: Store, agenda, afspraak_id: str) -> str:
    """Het gekozen moment wordt een echte afspraak; de andere voorlopige momenten van dit verzoek gaan weg."""
    v = store.voorlopig(afspraak_id)
    if v is None:
        raise ValueError("this isn't a tentative time held by the app")
    melding = agenda.bevestig_voorlopig(afspraak_id, v["titel"])
    weg = 0
    for ander in store.voorlopige(v["item_id"]):
        if ander["afspraak_id"] != afspraak_id:
            try:
                agenda.verwijder_afspraak(ander["afspraak_id"])
            except Exception as e:
                log.warning("voorlopig moment niet weg: %s", e)
            store.wis_voorlopig(ander["afspraak_id"])
            weg += 1
    store.wis_voorlopig(afspraak_id)
    return melding + ((f"; {weg} other times released" if weg != 1 else "; 1 other time released") if weg else "")


def geef_voorlopig_vrij(store: Store, agenda, afspraak_id: str) -> str:
    if store.voorlopig(afspraak_id) is None:
        raise ValueError("this isn't a tentative time held by the app")
    agenda.verwijder_afspraak(afspraak_id)
    store.wis_voorlopig(afspraak_id)
    return "Time released"


def deadline_in_agenda(store: Store, agenda, soort: str, rij_id: int) -> str:
    """Deadline van een taak of mail als hele-dag-afspraak in je agenda. Alleen na een klik."""
    from datetime import date as _date
    if agenda is None:
        raise ValueError("no calendar connected")
    if soort == "taak":
        rij = store.taak(rij_id)
        if rij is None:
            raise ValueError("task not found")
        titel, item = rij["omschrijving"], store.item(rij["item_id"]) if rij["item_id"] else None
    else:
        rij = item = store.item(rij_id)
        if rij is None:
            raise ValueError("email not found")
        titel = rij["samenvatting"] or rij["onderwerp"]
    if not rij["deadline"]:
        raise ValueError("there is no deadline")
    if rij["deadline_in_agenda"]:
        raise ValueError("this deadline is already in your calendar")
    try:
        dag = _date.fromisoformat(rij["deadline"])
    except ValueError:
        raise ValueError(f"deadline '{rij['deadline']}' is not a date") from None
    notities = f"Regarding: {item['onderwerp']} ({item['van']})" if item is not None else ""
    melding = agenda.maak_deadline(f"Deadline: {titel}", dag, notities)
    store.zet_deadline_in_agenda(soort, rij_id, dag.isoformat())
    return melding


def ochtend(cfg: Config, store: Store, agenda=None) -> dict:
    """Overzicht voor het begin van de dag: wat er vandaag staat, wat eraan komt en wat blijft liggen."""
    from datetime import date as _date
    from .agenda import lees_werktijd
    nu_ = datetime.now().astimezone()
    vandaag = nu_.date()
    over_week = vandaag + timedelta(days=7)
    werkdagen = lees_werktijd(cfg.agenda).dagen
    open_ = store.open_taken()

    def datum(s):
        try:
            return _date.fromisoformat((s or "")[:10])
        except ValueError:
            return None

    ingepland = [{"id": t["id"], "omschrijving": t["omschrijving"], "gepland": t["gepland"], "minuten": t["minuten"] or 30}
                 for t in open_ if t["gepland"] and datum(t["gepland"]) == vandaag]
    ingepland.sort(key=lambda t: t["gepland"])
    deadlines = []
    for t in open_:
        d = datum(t["deadline"])
        if d and d <= over_week:
            deadlines.append({"soort": "taak", "id": t["id"], "titel": t["omschrijving"], "deadline": d.isoformat(),
                              "ingepland": bool(t["gepland"]), "in_agenda": bool(t["deadline_in_agenda"]),
                              "te_laat": d < vandaag})
    te_doen = [store.item(r["id"]) for st in ("wacht", "fout", "handmatig") for r in store.items(status=st)]
    for it in te_doen:
        d = datum(it["deadline"])
        if d and d <= over_week:
            deadlines.append({"soort": "mail", "id": it["id"], "titel": it["onderwerp"] or it["samenvatting"],
                              "deadline": d.isoformat(), "ingepland": False, "in_agenda": bool(it["deadline_in_agenda"]),
                              "te_laat": d < vandaag})
    deadlines.sort(key=lambda d: d["deadline"])
    grens = plus_werkdagen(vandaag, 0, werkdagen)
    for _ in range(2):                    # twee werkdagen terug
        grens -= timedelta(days=1)
        while grens.isoweekday() not in werkdagen:
            grens -= timedelta(days=1)
    blijft_liggen = [{"id": it["id"], "van": it["van"], "onderwerp": it["onderwerp"],
                      "dagen": (vandaag - datum(it["datum"])).days}
                     for it in te_doen if datum(it["datum"]) and datum(it["datum"]) < grens]
    blijft_liggen.sort(key=lambda x: -x["dagen"])
    zonder_moment = [t for t in open_ if not t["gepland"] and not t["later"] and datum(t["deadline"])
                     and datum(t["deadline"]) <= over_week]
    afspraken = []
    if agenda is not None and hasattr(agenda, "afspraken"):
        try:
            begin = datetime.combine(vandaag, datetime.min.time()).astimezone()
            afspraken = [{"titel": a["titel"], "start": a["start"].isoformat(timespec="minutes"),
                          "heledag": a["heledag"]} for a in agenda.afspraken(begin, begin + timedelta(days=1))
                         if not a.get("vrij")]
            afspraken.sort(key=lambda a: a["start"])
        except Exception as e:
            log.warning("ochtendoverzicht: agenda niet te lezen: %s", e)
    wacht = [w for w in store.wacht_op_lijst("wacht") if w["herinner_op"] <= vandaag.isoformat()]
    gedaan = sum(1 for t in open_ if t["lijkt_klaar"]) + sum(1 for it in te_doen if it["lijkt_beantwoord"])
    belangrijk = sum(1 for it in te_doen if is_belangrijk(cfg, store, it["van"]))
    return {"datum": vandaag.isoformat(), "afspraken": afspraken, "ingepland": ingepland, "deadlines": deadlines[:8],
            "blijft_liggen": blijft_liggen[:5], "blijft_liggen_n": len(blijft_liggen),
            "zonder_moment": [t["id"] for t in zonder_moment], "wacht_op": len(wacht), "lijkt_gedaan": gedaan,
            "belangrijk": belangrijk}


def ochtend_tekst(o: dict) -> str:
    """Eén of twee zinnen voor de melding om 07:45."""
    delen = []
    tijden = [a for a in o["afspraken"] if not a["heledag"]]
    if tijden:
        delen.append((f"{len(tijden)} events" if len(tijden) != 1 else "1 event") + f", the first at {tijden[0]['start'][11:16]}")
    if o["ingepland"]:
        delen.append(f"{len(o['ingepland'])} " + ("task" if len(o["ingepland"]) == 1 else "tasks") + " scheduled")
    if o["deadlines"]:
        delen.append(f"{len(o['deadlines'])} " + ("deadline" if len(o["deadlines"]) == 1 else "deadlines") + " this week")
    if o["belangrijk"]:
        delen.append(f"{o['belangrijk']} from important senders")
    if o["blijft_liggen_n"]:
        delen.append(f"{o['blijft_liggen_n']} " + ("email has" if o["blijft_liggen_n"] == 1 else "emails have") + " been waiting two working days")
    if o["wacht_op"]:
        delen.append(f"{o['wacht_op']} " + ("reply" if o["wacht_op"] == 1 else "replies") + " still outstanding")
    if o["lijkt_gedaan"]:
        delen.append(f"{o['lijkt_gedaan']} " + ("looks" if o["lijkt_gedaan"] == 1 else "look") + " done already")
    if not delen:
        return "Nothing urgent today."
    zin = ", ".join(delen) + "."
    return zin[0].upper() + zin[1:]


def schone_taken(taken: list[dict]) -> list[dict]:
    """Taken uit de app of van Claude: lege weg, minuten tussen 15 en 480, 'antwoord' voor de antwoordtaak."""
    schoon = []
    for t in taken:
        tekst = (t.get("omschrijving") or "").strip()
        if not tekst:
            continue
        try:
            minuten = int(t.get("minuten") or 0) or None
        except (TypeError, ValueError):
            minuten = None
        if minuten is not None:
            minuten = max(15, min(480, minuten))
        schoon.append({"omschrijving": tekst, "deadline": (t.get("deadline") or None), "minuten": minuten,
                       "antwoord": bool(t.get("antwoord"))})
    return schoon


def zet_taken_klaar(store: Store, bronnen: dict, item_id: int, concept: str | None, taken: list[dict],
                    vlag: int | None = None) -> tuple[str, list[int]]:
    """Taken uit een mail naar je planning, zonder nu te antwoorden. Het concept blijft bij de mail,
    zodat Beantwoorden in Planning het klaar heeft staan. Alleen na een klik."""
    item = store.item(item_id)
    if item is None:
        raise ValueError(f"Item {item_id} does not exist")
    if item["status"] not in ("wacht", "fout", "handmatig"):
        raise ValueError("this email has already been handled")
    taken = schone_taken(taken)
    if not taken:
        raise ValueError("there are no tasks in the list")
    factor, n = store.tijdfactor(item["project"])
    for t in taken:   # je eigen ervaring met schattingen in dit project
        if t["minuten"] and n >= 3 and abs(factor - 1) >= 0.1:
            t["minuten"] = max(15, min(480, round(t["minuten"] * factor / 15) * 15))
    if concept is not None:
        store.zet_concept(item_id, concept.strip())
    ids = store.voeg_taken_toe(item_id, item["project"], taken)
    store.bewaar_vorige_status(item_id, item["status"])
    store.zet_status(item_id, "taak", opmerking="tasks prepared; the email stays in the inbox")
    store.log(item_id, f"{len(ids)} taken klaargezet")
    melding = ("1 task" if len(ids) == 1 else f"{len(ids)} tasks") + " ready in Planning"
    if vlag is not None:
        fout = markeer_mail(store, bronnen, item_id, True, vlag)
        if fout:
            melding += f"; {fout}"
    return melding, ids


def zet_taken_klaar_terug(store: Store, bronnen: dict, item_id: int, taak_ids: list[int],
                          vlag: int | None = None) -> str:
    """Ongedaan maken van Taken klaarzetten: taken weg (zolang ze open en niet ingepland zijn), mail terug."""
    item = store.item(item_id)
    if item is None or item["status"] != "taak":
        raise ValueError("this email is no longer a task")
    eigen = {t["id"] for t in store.open_taken() if t["item_id"] == item_id}
    store.verwijder_open_taken([i for i in taak_ids if i in eigen])
    store.zet_status(item_id, item["vorige_status"] or "wacht")
    if vlag is not None and not any(t["item_id"] == item_id for t in store.open_taken()):
        markeer_mail(store, bronnen, item_id, False, vlag)
    return "Back in To do"


def markeer_gedaan(store: Store, item_id: int) -> str:
    """Geen antwoord nodig: uit Te doen, de mail blijft waar hij staat."""
    item = store.item(item_id)
    if item is None:
        raise ValueError("email not found")
    store.bewaar_vorige_status(item_id, item["status"])
    store.zet_status(item_id, "gedaan")
    return "Done"


def zet_gedaan_terug(store: Store, item_id: int) -> str:
    item = store.item(item_id)
    if item is None or item["status"] != "gedaan":
        raise ValueError("not marked as done")
    store.zet_status(item_id, item["vorige_status"] or "wacht")
    return "Back in To do"


def terug_naar_te_doen(store: Store, bronnen: dict, item_id: int) -> str:
    """Een afgehandelde mail weer in Te doen zetten, om er toch nog iets mee te doen. Gearchiveerde,
    opgeborgen of weggegooide mail gaat eerst terug naar de inbox. Al verstuurde antwoorden blijven verstuurd."""
    it = store.item(item_id)
    if it is None:
        raise ValueError("email not found")
    if it["status"] in ("gearchiveerd", "opgeborgen", "verwijderd"):
        melding = zet_terug(store, bronnen, [item_id])
        if "failed" in melding:
            raise ValueError(melding)
        return "Back in your inbox and in To do"
    if it["status"] in ("wacht", "fout", "handmatig"):
        return "Already in To do"
    if it["opgeruimd"] and it["opgeruimd"] != "weg":   # na je antwoord gearchiveerd: eerst terug naar de inbox
        bron = bronnen.get(it["bron"])
        if bron is None or not hasattr(bron, "terugzetten") or not it["thread_id"]:
            raise ValueError("couldn't bring the email back from the archive")
        nieuw = bron.terugzetten([it["thread_id"]], "archief").get(it["thread_id"], "FOUT:no response")
        if nieuw.startswith("FOUT"):
            raise ValueError(f"couldn't bring the email back from the archive ({nieuw[5:]})")
        if nieuw != it["bron_id"]:
            store.nieuw_bron_id(it["sleutel"], it["bron"], nieuw)
        store.zet_opgeruimd(item_id, None)
    terug = it["vorige_status"] if it["vorige_status"] in ("wacht", "fout", "handmatig") else "wacht"
    store.zet_status(item_id, terug)
    store.wis_lijkt_beantwoord(item_id)
    return ("Back in To do; your reply had already been sent" if it["status"] == "verzonden" else "Back in To do")


def wijs_af(store: Store, item_id: int, reden: str | None = None) -> None:
    store.zet_status(item_id, "afgewezen", opmerking=reden or None)


def overzicht(cfg: Config, store: Store, uren: int = 24) -> str:
    """Overzicht in Markdown, per project."""
    sinds = (datetime.now().astimezone() - timedelta(hours=uren)).isoformat(timespec="seconds")
    regels = [f"# Email overview, {datetime.now():%A} {datetime.now().day} {datetime.now():%B %Y, %H:%M}", ""]
    for p in cfg.projecten:
        wacht = store.items(status="wacht", project=p)
        fout = store.items(status="fout", project=p) + store.items(status="handmatig", project=p)
        info = store.items(status="ter_info", project=p, sinds=sinds)
        taken = store.open_taken(p)
        if not (wacht or fout or info or taken):
            continue
        regels.append("## " + {"onderwijs": "Teaching", "onderzoek": "Research", "istte": "ISTTE", "overig": "Other"}.get(p, p.capitalize()))
        if wacht:
            regels.append(f"**Waiting for you ({len(wacht)})**")
            for i in wacht:
                dl = f", deadline {i['deadline']}" if i["deadline"] else ""
                regels.append(f"- [{i['prioriteit']}{dl}] {i['samenvatting']} ({i['van']})")
        if fout:
            regels.append(f"**Check yourself ({len(fout)})**")
            for i in fout:
                regels.append(f"- {i['onderwerp']} ({i['van']}): {i['opmerking'] or i['reden']}")
        if taken:
            regels.append(f"**Open tasks ({len(taken)})**")
            for t in taken:
                regels.append(f"- {t['omschrijving']}" + (f" (by {t['deadline']})" if t["deadline"] else ""))
        if info:
            regels.append(f"**FYI, last {uren} hours ({len(info)})**")
            for i in info:
                regels.append(f"- {i['samenvatting']}")
        regels.append("")
    if len(regels) == 2:
        regels.append("Nothing new.")
    return "\n".join(regels)
