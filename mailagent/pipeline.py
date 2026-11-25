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
from .models import UITWERK_ACTIES, Email, Sortering
from .store import Store

log = logging.getLogger("mailagent")

OPEN_PLEK = re.compile(r"\[\s*AANVULLEN", re.IGNORECASE)   # plek die Claude voor jou openliet


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
    """Verwerkt één mail en geeft het id van het item in de wachtrij terug.

    De mail wordt pas als verwerkt opgeslagen samen met zijn item (zie Store.nieuw_item)."""
    vast_project, sortering = sorteer_met_regels(cfg, mail)
    belangrijk = is_belangrijk(cfg, store, mail.van)

    geleerd = store.afzender_regel(afzender_adres(mail.van))
    if sortering is None and geleerd is not None and not belangrijk:
        s = Sortering(project=geleerd["project"] or vast_project or "overig", actie=geleerd["actie"],
                      prioriteit="laag", deadline=None, samenvatting=mail.onderwerp,
                      reden="afzender staat op je lijst 'voortaan overslaan'")
        return store.nieuw_item(mail, s, None, "genegeerd")

    if not mag_naar_claude(cfg, mail):
        s = Sortering(project=vast_project or "overig", actie="antwoorden", prioriteit="hoog" if belangrijk else "normaal",
                      deadline=None, samenvatting=mail.onderwerp, reden="staat op de lijst 'nooit naar Claude'")
        return store.nieuw_item(mail, s, None, "handmatig")

    if cfg.bulk_overslaan and mail.extra.get("bulk") and vast_project is None and sortering is None and not belangrijk:
        s = Sortering(project="overig", actie="negeren", prioriteit="laag", deadline=None,
                      samenvatting=mail.onderwerp, reden="nieuwsbrief of reclame (afmeldlink); niet naar Claude")
        return store.nieuw_item(mail, s, None, "genegeerd")

    tekst = anonimiseer(cfg, mail.tekst)
    uitwerking, context = None, []
    try:
        if sortering is None:
            sortering = llm.sorteer(mail, tekst, vast_project)
        if sortering.actie in UITWERK_ACTIES:
            extra, context = achtergrond(cfg, store, sleutel=mail.sleutel, van=mail.van, onderwerp=mail.onderwerp,
                                         thread_id=mail.thread_id, verwijzingen=mail.extra.get("verwijzingen"),
                                         datum=mail.datum)
            uitwerking = llm.werk_uit(mail, tekst, sortering, stijlvoorbeelden(cfg, store, mail, sortering.project),
                                      achtergrond=extra or None)
    except Exception as e:  # ook een kapot antwoord of een onverwachte fout: de mail komt bij 'zelf doen'
        soort = "Claude-fout" if isinstance(e, LLMFout) else f"onverwachte fout ({type(e).__name__})"
        log.warning("%s bij %s: %s", soort, mail.sleutel, e)
        s = sortering or Sortering(project=vast_project or "overig", actie="antwoorden", prioriteit="normaal",
                                   deadline=None, samenvatting=mail.onderwerp, reden="niet gesorteerd")
        return store.nieuw_item(mail, s, None, "fout", opmerking=f"{soort}: {e}"[:500])
    if belangrijk:   # nooit missen: altijd in Te doen, bovenaan
        sortering = sortering.model_copy(update={"prioriteit": "hoog",
                                                 "reden": f"{sortering.reden} · belangrijke afzender".strip(" ·")})
    if sortering.actie in UITWERK_ACTIES:
        item_id = store.nieuw_item(mail, sortering, uitwerking, "wacht")
        if context:
            store.zet_context(item_id, context)
        return item_id
    if belangrijk:
        return store.nieuw_item(mail, sortering, None, "wacht")
    status = "genegeerd" if sortering.actie == "negeren" else "ter_info"
    return store.nieuw_item(mail, sortering, None, status)


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
    project = (item["project"] or "").lower()
    for m in mappen:
        if project and project != "overig" and m.split("/")[-1].strip().lower() == project:
            score[m] = score.get(m, 0) + 3
            reden.setdefault(m, "zelfde naam als het project")
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
    if t["gepland"]:
        raise ValueError("deze taak staat al in je agenda")   # geen dubbele afspraken
    if t["klaar"]:
        raise ValueError("deze taak is al afgevinkt")
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


class RondeBezig(RuntimeError):
    """Er loopt al een mailronde (gepland of vanuit de app)."""


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
            raise RondeBezig("Er loopt al een mailronde. Probeer het over een paar minuten opnieuw.") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run(cfg: Config, store: Store, bronnen: dict, llm: LLM, dagen: int | None = None,
        maximum: int | None = None, voortgang: dict | None = None) -> dict[str, int]:
    """Verwerkt per bron de mail van de afgelopen `terugkijken_dagen` die nog niet verwerkt is, oudste eerst.

    Elke ronde neemt hoogstens `maximum` mails per bron; de volgende ronde gaat verder.
    Al verwerkte mail wordt herkend en niet opnieuw naar Claude gestuurd.
    `voortgang` krijgt per bron het aantal mails dat daarna nog te gaan is.
    """
    with ronde_slot(store):
        resultaat: dict[str, int] = {}
        uitgaand: dict[str, list] = {}   # per bron: je verstuurde mail van de laatste 30 dagen
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
                    item_id = verwerk_mail(cfg, store, llm, mail)
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
        return resultaat


def keur_goed(store: Store, bronnen: dict, item_id: int, concept: str | None,
              taken: list[dict] | None = None, verzenden: bool = False) -> str:
    """Goedkeuren: concept in Mail zetten (of na jouw bevestigde klik meteen versturen) en taken opslaan.
    Versturen gebeurt alleen vanuit de app na een klik van de gebruiker, nooit tijdens een ronde."""
    item = store.item(item_id)
    if item is None:
        raise ValueError(f"Item {item_id} bestaat niet")
    if item["status"] not in ("wacht", "fout", "handmatig"):
        raise ValueError(f"Item {item_id} is al afgehandeld ({item['status']})")

    meldingen = []
    concept = (concept or "").strip() or None
    if verzenden and not concept:
        raise ValueError("geen tekst om te versturen")
    if verzenden and OPEN_PLEK.search(concept):
        raise ValueError("het concept bevat nog [AANVULLEN: …]; vul dat eerst in")
    verzonden = False
    if concept:
        bron = bronnen.get(item["bron"])
        if bron is None:
            raise ValueError(f"Bron '{item['bron']}' staat niet meer in de configuratie")
        store.zet_concept(item_id, concept)   # jouw tekst blijft bewaard, ook als Mail hierna faalt
        if verzenden:
            uitkomst = bron.maak_concept(item["bron_id"], concept, item["onderwerp"], item["van"],
                                         message_id=item["thread_id"], verzenden=True)
            verzonden = getattr(bron, "laatste_uitkomst", None) == "verzonden"
            if not verzonden:
                # niet aantoonbaar verstuurd: de mail blijft in Te doen, met jouw tekst bewaard
                store.log(item_id, f"verzenden niet bevestigd: {uitkomst}")
                return uitkomst
            meldingen.append(f"verzonden aan {item['van']}")
        else:
            meldingen.append(bron.maak_concept(item["bron_id"], concept, item["onderwerp"], item["van"],
                                               message_id=item["thread_id"]))
    if taken is None:   # niet aangepast: de taken zoals Claude ze voorstelde
        taken = json.loads(item["taken"] or "[]")
    taken = [t for t in schone_taken(taken) if not t["antwoord"]]   # je antwoordt nu, dus die taak vervalt
    if taken:
        store.voeg_taken_toe(item_id, item["project"], taken)
        meldingen.append("1 taak toegevoegd" if len(taken) == 1 else f"{len(taken)} taken toegevoegd")
    melding = "; ".join(meldingen) or "goedgekeurd zonder concept of taken"
    store.zet_status(item_id, "verzonden" if verzonden else "goedgekeurd", concept=concept, opmerking=melding)
    if concept:
        store.zet_eigen_antwoord(item_id, concept)
    store.log(item_id, "VERZONDEN na bevestigde klik" if verzonden else "concept in Mail gezet")
    return melding


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
            if not antwoorden or it["bewijs"] == antwoorden[-1]["message_id"]:
                continue
            v = antwoorden[-1]
            soort = "doorgestuurd" if DOORSTUREN.match(v["onderwerp"] or "") else "beantwoord"
            ontvanger = v["aan"].split(",")[0].strip() or "de afzender"
            store.zet_lijkt_beantwoord(it["id"], f"Je hebt deze mail {soort} op {v['datum'].astimezone():%d-%m %H:%M} "
                                       f"(aan {ontvanger})", v["message_id"])
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
    ontvanger = laatste["aan"].split(",")[0].strip() or "de afzender"
    wanneer = f"{laatste['datum'].astimezone():%d-%m %H:%M}"
    nieuw, te_vragen = 0, []
    for t in taken:
        if t["bewijs"] == bewijs:
            continue          # al bekeken met dit antwoord (of door jou op 'nog niet' gezet)
        if t["soort"] == "antwoord":
            soort = "doorgestuurd" if DOORSTUREN.match(laatste["onderwerp"] or "") else "beantwoord"
            store.zet_lijkt_klaar(t["id"], f"Je hebt deze mail {soort} op {wanneer} (aan {ontvanger})", bewijs)
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
            store.zet_lijkt_klaar(t["id"], f"Je mail van {wanneer} aan {ontvanger}: {klaar[t['id']]}", bewijs)
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


def deadline_in_agenda(store: Store, agenda, soort: str, rij_id: int) -> str:
    """Deadline van een taak of mail als hele-dag-afspraak in je agenda. Alleen na een klik."""
    from datetime import date as _date
    if agenda is None:
        raise ValueError("geen agenda gekoppeld")
    if soort == "taak":
        rij = store.taak(rij_id)
        if rij is None:
            raise ValueError("taak niet gevonden")
        titel, item = rij["omschrijving"], store.item(rij["item_id"]) if rij["item_id"] else None
    else:
        rij = item = store.item(rij_id)
        if rij is None:
            raise ValueError("mail niet gevonden")
        titel = rij["samenvatting"] or rij["onderwerp"]
    if not rij["deadline"]:
        raise ValueError("er is geen deadline")
    if rij["deadline_in_agenda"]:
        raise ValueError("deze deadline staat al in je agenda")
    try:
        dag = _date.fromisoformat(rij["deadline"])
    except ValueError:
        raise ValueError(f"deadline '{rij['deadline']}' is geen datum") from None
    notities = f"Naar aanleiding van: {item['onderwerp']} ({item['van']})" if item is not None else ""
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
        delen.append(f"{len(tijden)} afspraken, eerste om {tijden[0]['start'][11:16]}")
    if o["ingepland"]:
        delen.append(f"{len(o['ingepland'])} taken ingepland")
    if o["deadlines"]:
        delen.append(f"{len(o['deadlines'])} deadlines deze week")
    if o["belangrijk"]:
        delen.append(f"{o['belangrijk']} van belangrijke afzenders")
    if o["blijft_liggen_n"]:
        delen.append(f"{o['blijft_liggen_n']} mails liggen al twee werkdagen")
    if o["wacht_op"]:
        delen.append(f"{o['wacht_op']} keer geen antwoord")
    if o["lijkt_gedaan"]:
        delen.append(f"{o['lijkt_gedaan']} lijken al gedaan")
    return ", ".join(delen) + "." if delen else "Niets dringends vandaag."


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
        raise ValueError(f"Item {item_id} bestaat niet")
    if item["status"] not in ("wacht", "fout", "handmatig"):
        raise ValueError("deze mail is al afgehandeld")
    taken = schone_taken(taken)
    if not taken:
        raise ValueError("er staan geen taken in de lijst")
    factor, n = store.tijdfactor(item["project"])
    for t in taken:   # je eigen ervaring met schattingen in dit project
        if t["minuten"] and n >= 3 and abs(factor - 1) >= 0.1:
            t["minuten"] = max(15, min(480, round(t["minuten"] * factor / 15) * 15))
    if concept is not None:
        store.zet_concept(item_id, concept.strip())
    ids = store.voeg_taken_toe(item_id, item["project"], taken)
    store.bewaar_vorige_status(item_id, item["status"])
    store.zet_status(item_id, "taak", opmerking="taken klaargezet; mail blijft in de inbox")
    store.log(item_id, f"{len(ids)} taken klaargezet")
    melding = ("1 taak" if len(ids) == 1 else f"{len(ids)} taken") + " klaargezet in Planning"
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
        raise ValueError("deze mail staat niet meer als taak")
    eigen = {t["id"] for t in store.open_taken() if t["item_id"] == item_id}
    store.verwijder_open_taken([i for i in taak_ids if i in eigen])
    store.zet_status(item_id, item["vorige_status"] or "wacht")
    if vlag is not None and not any(t["item_id"] == item_id for t in store.open_taken()):
        markeer_mail(store, bronnen, item_id, False, vlag)
    return "Terug in Te doen"


def markeer_gedaan(store: Store, item_id: int) -> str:
    """Geen antwoord nodig: uit Te doen, de mail blijft waar hij staat."""
    item = store.item(item_id)
    if item is None:
        raise ValueError("mail niet gevonden")
    store.bewaar_vorige_status(item_id, item["status"])
    store.zet_status(item_id, "gedaan")
    return "Gedaan"


def zet_gedaan_terug(store: Store, item_id: int) -> str:
    item = store.item(item_id)
    if item is None or item["status"] != "gedaan":
        raise ValueError("niet als gedaan gemarkeerd")
    store.zet_status(item_id, item["vorige_status"] or "wacht")
    return "Terug in Te doen"


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
