"""De workflow: ophalen -> lokale regels -> sorteren -> uitwerken -> wachtrij -> goedkeuren."""

from __future__ import annotations

import json
import logging
from email.utils import parseaddr
from datetime import datetime, timedelta

from .config import Config, anonimiseer, mag_naar_claude, sorteer_met_regels
from .llm import LLM, LLMFout
from .models import UITWERK_ACTIES, Email, Sortering
from .store import Store

log = logging.getLogger("mailagent")


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


def verwerk_mail(cfg: Config, store: Store, llm: LLM, mail: Email) -> int:
    """Verwerkt één mail en geeft het id van het item in de wachtrij terug."""
    store.bewaar_mail(mail)
    vast_project, sortering = sorteer_met_regels(cfg, mail)

    geleerd = store.afzender_regel(afzender_adres(mail.van))
    if sortering is None and geleerd is not None:
        s = Sortering(project=geleerd["project"] or vast_project or "overig", actie=geleerd["actie"],
                      prioriteit="laag", deadline=None, samenvatting=mail.onderwerp,
                      reden="afzender staat op je lijst 'voortaan overslaan'")
        return store.nieuw_item(mail, s, None, "genegeerd")

    if not mag_naar_claude(cfg, mail):
        s = Sortering(project=vast_project or "overig", actie="antwoorden", prioriteit="normaal",
                      deadline=None, samenvatting=mail.onderwerp, reden="staat op de lijst 'nooit naar Claude'")
        return store.nieuw_item(mail, s, None, "handmatig")

    if cfg.bulk_overslaan and mail.extra.get("bulk") and vast_project is None and sortering is None:
        s = Sortering(project="overig", actie="negeren", prioriteit="laag", deadline=None,
                      samenvatting=mail.onderwerp, reden="nieuwsbrief of reclame (afmeldlink); niet naar Claude")
        return store.nieuw_item(mail, s, None, "genegeerd")

    tekst = anonimiseer(cfg, mail.tekst)
    try:
        if sortering is None:
            sortering = llm.sorteer(mail, tekst, vast_project)
        if sortering.actie in UITWERK_ACTIES:
            uitwerking = llm.werk_uit(mail, tekst, sortering, stijlvoorbeelden(cfg, store, mail, sortering.project))
            return store.nieuw_item(mail, sortering, uitwerking, "wacht")
        status = "genegeerd" if sortering.actie == "negeren" else "ter_info"
        return store.nieuw_item(mail, sortering, None, status)
    except LLMFout as e:
        log.warning("Claude-fout bij %s: %s", mail.sleutel, e)
        s = sortering or Sortering(project=vast_project or "overig", actie="antwoorden", prioriteit="normaal",
                                   deadline=None, samenvatting=mail.onderwerp, reden="niet gesorteerd")
        return store.nieuw_item(mail, s, None, "fout", opmerking=str(e))


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
            reden[r["map"]] = f"{r['aantal']}× eerder voor deze afzender"
        elif r["project"] and r["project"] == item["project"]:
            score[r["map"]] = score.get(r["map"], 0) + r["aantal"]
            reden.setdefault(r["map"], f"vaker gebruikt voor {item['project']}")
    woorden = {w for w in re.findall(r"[a-z0-9]+", (item["onderwerp"] or "").lower()) if len(w) >= 4}
    for m in mappen:
        mapwoorden = {w for w in re.findall(r"[a-z0-9]+", m.split("/")[-1].lower()) if len(w) >= 4}
        if any(w.startswith(mw) or mw.startswith(w) for w in woorden for mw in mapwoorden):
            score[m] = score.get(m, 0) + 5
            reden.setdefault(m, "naam past bij het onderwerp")
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
            mislukt += [f"{it['onderwerp'][:40]} (bron {bron_naam} kan niet verplaatsen)" for it in items]
            continue
        try:
            res = bron.verplaats([it["bron_id"] for it in items], doel)
        except Exception as e:
            mislukt += [f"{it['onderwerp'][:40]} ({e})" for it in items]
            continue
        for it in items:
            fout = res.get(it["bron_id"], "geen resultaat")
            if fout is None:
                store.bewaar_vorige_status(it["id"], it["status"])
                store.zet_status(it["id"], nieuwe_status, opmerking=doel[4:] if doel.startswith("map:") else None)
                if doel.startswith("map:"):
                    store.onthoud_map(bron_naam, doel[4:], afzender_adres(it["van"] or ""), it["project"])
                gelukt += 1
            else:
                mislukt.append(f"{it['onderwerp'][:40]} ({fout})")

    waar = ("het archief" if doel == "archief" else "de prullenbak (terug te halen in Apple Mail)"
            if doel in ("archief", "prullenbak") else f"map {doel[4:]}")
    melding = f"{gelukt} mail(s) verplaatst naar {waar}"
    if mislukt:
        melding += f"; niet gelukt: {'; '.join(mislukt[:5])}"
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
        return f"vlag in Mail {'zetten' if aan else 'weghalen'} lukte niet ({e})"


def maak_taak(store: Store, bronnen: dict, agenda, item_id: int, titel: str, stappen: list[str],
              minuten: int, deadline: str | None, start: datetime | None, archiveren: bool,
              vlag: int | None = 4) -> str:
    """Taak opslaan, eventueel inplannen in de agenda en de mail archiveren. Alleen na een klik."""
    item = store.item(item_id)
    if item is None:
        raise ValueError(f"Item {item_id} bestaat niet")
    meldingen = []
    gepland = None
    if start is not None:
        if agenda is None:
            raise ValueError("geen agenda gekoppeld; maak de taak zonder inplannen")
        notities = "\n".join(f"- {s}" for s in stappen) + f"\n\nNaar aanleiding van: {item['onderwerp']} ({item['van']})"
        meldingen.append(agenda.maak_afspraak(afspraak_titel(titel, stappen), start, start + timedelta(minutes=minuten), notities))
        gepland = start.isoformat(timespec="minutes")
    store.nieuwe_taak(item_id, item["project"], titel, deadline, minuten, stappen, gepland)
    meldingen.insert(0, "Taak gemaakt, mail gekoppeld")
    if vlag is not None:   # eerst markeren, dan pas eventueel archiveren
        fout = markeer_mail(store, bronnen, item_id, True, vlag)
        meldingen.append(fout or "gemarkeerd als ingepland in Mail")
    if archiveren and item["status"] in ("ter_info", "genegeerd"):
        meldingen.append(verplaats_items(store, bronnen, [item_id], "archief"))
    elif item["status"] in ("ter_info", "genegeerd", "wacht", "fout", "handmatig"):
        # weg uit Beoordelen en Opruimen; het concept-antwoord blijft bewaard en staat klaar bij Beantwoorden
        store.zet_status(item_id, "taak", opmerking="omgezet in taak; mail blijft in de inbox")
    return "; ".join(meldingen)


def plan_bestaande_taak(store: Store, agenda, taak_id: int, start: datetime, minuten: int) -> str:
    """Een open taak in de agenda zetten (vanuit Planning). Alleen na een klik."""
    if agenda is None:
        raise ValueError("geen agenda gekoppeld")
    t = store.taak(taak_id)
    if t is None:
        raise ValueError("taak niet gevonden")
    stappen = json.loads(t["stappen"] or "[]")
    notities = "\n".join(f"- {s}" for s in stappen)
    if t["item_id"]:
        it = store.item(t["item_id"])
        if it is not None:
            notities += f"\n\nNaar aanleiding van: {it['onderwerp']} ({it['van']})"
    melding = agenda.maak_afspraak(afspraak_titel(t["omschrijving"], stappen), start,
                                   start + timedelta(minutes=minuten), notities.strip())
    store.plan_taak(taak_id, start.isoformat(timespec="minutes"), minuten)
    return melding


def open_antwoord(store: Store, bronnen: dict, item_id: int, tekst: str) -> str:
    """Antwoordvenster openen voor de mail achter een taak. Verandert niets aan taak of status."""
    item = store.item(item_id)
    if item is None:
        raise ValueError("mail niet gevonden")
    bron = bronnen.get(item["bron"])
    if bron is None:
        raise ValueError(f"Bron '{item['bron']}' staat niet meer in de configuratie")
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
    melding = f"{gelukt} mail(s) terug in je inbox"
    if mislukt:
        melding += f"; niet gelukt: {'; '.join(mislukt[:3])}"
    return melding


def afspraak_titel(titel: str, stappen: list[str]) -> str:
    """Titel met de eerste concrete stap erin (als-dan-plan: om 14:00 begin ik met X)."""
    eerste = next((s.strip() for s in stappen if s.strip()), "")
    if not eerste:
        return titel
    return f"{titel} · begin met: {eerste[0].lower() + eerste[1:]}"


def run(cfg: Config, store: Store, bronnen: dict, llm: LLM, dagen: int | None = None,
        maximum: int | None = None, voortgang: dict | None = None) -> dict[str, int]:
    """Verwerkt per bron de mail van de afgelopen `terugkijken_dagen` die nog niet verwerkt is, oudste eerst.

    Elke ronde neemt hoogstens `maximum` mails per bron; de volgende ronde gaat verder.
    Al verwerkte mail wordt herkend en niet opnieuw naar Claude gestuurd.
    `voortgang` krijgt per bron het aantal mails dat daarna nog te gaan is.
    """
    resultaat: dict[str, int] = {}
    maximum = maximum or cfg.max_mails_per_run
    nu = datetime.now().astimezone()
    sinds = nu - timedelta(days=dagen or cfg.terugkijken_dagen)
    for naam, bron in bronnen.items():
        log.info("%s: mail van de afgelopen %d dagen doorzoeken…", naam, dagen or cfg.terugkijken_dagen)
        try:
            mails = bron.haal_op(sinds, maximum, overslaan=lambda bid, n=naam: store.is_bekend_sleutel(f"{n}:{bid}"))
        except Exception as e:  # een kapotte bron mag de andere niet tegenhouden
            log.error("Ophalen uit %s mislukt: %s", naam, e)
            store.log(None, f"ophalen {naam} mislukt: {e}")
            resultaat[naam] = -1
            continue

        totaal = getattr(bron, "laatste_totaal", len(mails))
        te_doen = [m for m in sorted(mails, key=lambda m: m.datum) if not store.is_bekend(m)]
        log.info("%s: %d onverwerkte mail(s) sinds %s, deze ronde %d", naam, totaal, f"{sinds:%d-%m}", len(te_doen))
        nieuw = 0
        for i, mail in enumerate(te_doen, 1):
            log.info("%s %d/%d: %s", naam, i, len(te_doen), mail.onderwerp[:70])
            item_id = verwerk_mail(cfg, store, llm, mail)
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
    return resultaat


def keur_goed(store: Store, bronnen: dict, item_id: int, concept: str | None,
              taken: list[dict] | None = None) -> str:
    """Goedkeuren: concept klaarzetten in de mailbox (niet versturen) en taken opslaan."""
    item = store.item(item_id)
    if item is None:
        raise ValueError(f"Item {item_id} bestaat niet")
    if item["status"] not in ("wacht", "fout", "handmatig"):
        raise ValueError(f"Item {item_id} is al afgehandeld ({item['status']})")

    meldingen = []
    concept = (concept or "").strip() or None
    if concept:
        bron = bronnen.get(item["bron"])
        if bron is None:
            raise ValueError(f"Bron '{item['bron']}' staat niet meer in de configuratie")
        meldingen.append(bron.maak_concept(item["bron_id"], concept, item["onderwerp"], item["van"],
                                           message_id=item["thread_id"]))
    if taken is None:   # niet aangepast: de taken zoals Claude ze voorstelde
        taken = json.loads(item["taken"] or "[]")
    taken = [{"omschrijving": t["omschrijving"].strip(), "deadline": (t.get("deadline") or None)}
             for t in taken if (t.get("omschrijving") or "").strip()]
    if taken:
        store.voeg_taken_toe(item_id, item["project"], taken)
        meldingen.append(f"{len(taken)} taak/taken toegevoegd")
    melding = "; ".join(meldingen) or "goedgekeurd zonder concept of taken"
    store.zet_status(item_id, "goedgekeurd", concept=concept, opmerking=melding)
    if concept:
        store.zet_eigen_antwoord(item_id, concept)
    return melding


def wijs_af(store: Store, item_id: int, reden: str | None = None) -> None:
    store.zet_status(item_id, "afgewezen", opmerking=reden or None)


def overzicht(cfg: Config, store: Store, uren: int = 24) -> str:
    """Overzicht in Markdown, per project."""
    sinds = (datetime.now().astimezone() - timedelta(hours=uren)).isoformat(timespec="seconds")
    regels = [f"# Mailoverzicht {datetime.now():%A %d %B %Y, %H:%M}", ""]
    for p in cfg.projecten:
        wacht = store.items(status="wacht", project=p)
        fout = store.items(status="fout", project=p) + store.items(status="handmatig", project=p)
        info = store.items(status="ter_info", project=p, sinds=sinds)
        taken = store.open_taken(p)
        if not (wacht or fout or info or taken):
            continue
        regels.append(f"## {p.upper() if p == 'istte' else p.capitalize()}")
        if wacht:
            regels.append(f"**Wacht op jou ({len(wacht)})**")
            for i in wacht:
                dl = f", deadline {i['deadline']}" if i["deadline"] else ""
                regels.append(f"- [{i['prioriteit']}{dl}] {i['samenvatting']} ({i['van']})")
        if fout:
            regels.append(f"**Zelf bekijken ({len(fout)})**")
            for i in fout:
                regels.append(f"- {i['onderwerp']} ({i['van']}): {i['opmerking'] or i['reden']}")
        if taken:
            regels.append(f"**Open taken ({len(taken)})**")
            for t in taken:
                regels.append(f"- {t['omschrijving']}" + (f" (voor {t['deadline']})" if t["deadline"] else ""))
        if info:
            regels.append(f"**Ter info, laatste {uren} uur ({len(info)})**")
            for i in info:
                regels.append(f"- {i['samenvatting']}")
        regels.append("")
    if len(regels) == 2:
        regels.append("Niets nieuws.")
    return "\n".join(regels)
