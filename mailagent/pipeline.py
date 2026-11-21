"""De workflow: ophalen -> lokale regels -> sorteren -> uitwerken -> wachtrij -> goedkeuren."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from .config import Config, anonimiseer, mag_naar_claude, sorteer_met_regels
from .llm import LLM, LLMFout
from .models import UITWERK_ACTIES, Email, Sortering
from .store import Store

log = logging.getLogger("mailagent")


def verwerk_mail(cfg: Config, store: Store, llm: LLM, mail: Email) -> int:
    """Verwerkt één mail en geeft het id van het item in de wachtrij terug."""
    store.bewaar_mail(mail)
    vast_project, sortering = sorteer_met_regels(cfg, mail)

    if not mag_naar_claude(cfg, mail):
        s = Sortering(project=vast_project or "overig", actie="antwoorden", prioriteit="normaal",
                      deadline=None, samenvatting=mail.onderwerp, reden="staat op de lijst 'nooit naar Claude'")
        return store.nieuw_item(mail, s, None, "handmatig")

    tekst = anonimiseer(cfg, mail.tekst)
    try:
        if sortering is None:
            sortering = llm.sorteer(mail, tekst, vast_project)
        if sortering.actie in UITWERK_ACTIES:
            uitwerking = llm.werk_uit(mail, tekst, sortering)
            return store.nieuw_item(mail, sortering, uitwerking, "wacht")
        status = "genegeerd" if sortering.actie == "negeren" else "ter_info"
        return store.nieuw_item(mail, sortering, None, status)
    except LLMFout as e:
        log.warning("Claude-fout bij %s: %s", mail.sleutel, e)
        s = sortering or Sortering(project=vast_project or "overig", actie="antwoorden", prioriteit="normaal",
                                   deadline=None, samenvatting=mail.onderwerp, reden="niet gesorteerd")
        return store.nieuw_item(mail, s, None, "fout", opmerking=str(e))


def run(cfg: Config, store: Store, bronnen: dict, llm: LLM) -> dict[str, int]:
    """Haalt per bron nieuwe mail op en verwerkt die. Geeft per bron het aantal nieuwe items."""
    resultaat: dict[str, int] = {}
    for naam, bron in bronnen.items():
        sinds = store.laatst_opgehaald(naam) or (
            datetime.now().astimezone() - timedelta(days=cfg.eerste_run_dagen_terug))
        try:
            mails = bron.haal_op(sinds, cfg.max_mails_per_run)
        except Exception as e:  # een kapotte bron mag de andere niet tegenhouden
            log.error("Ophalen uit %s mislukt: %s", naam, e)
            store.log(None, f"ophalen {naam} mislukt: {e}")
            resultaat[naam] = -1
            continue

        nieuw = 0
        nieuwste = sinds
        te_doen = [m for m in sorted(mails, key=lambda m: m.datum) if not store.is_bekend(m)]
        log.info("%s: %d opgehaald, %d nieuw te verwerken", naam, len(mails), len(te_doen))
        for mail in mails:
            nieuwste = max(nieuwste, mail.datum)
        for i, mail in enumerate(te_doen, 1):
            log.info("%s %d/%d: %s", naam, i, len(te_doen), mail.onderwerp[:70])
            item_id = verwerk_mail(cfg, store, llm, mail)
            item = store.item(item_id)
            log.info("   -> %s, %s (%s)", item["project"], item["actie"], item["status"])
            nieuw += 1
        store.zet_laatst_opgehaald(naam, nieuwste)
        store.log(None, f"run {naam}: {len(mails)} opgehaald, {nieuw} nieuw")
        resultaat[naam] = nieuw
    return resultaat


def keur_goed(store: Store, bronnen: dict, item_id: int, concept: str | None) -> str:
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
        meldingen.append(bron.maak_concept(item["bron_id"], concept, item["onderwerp"], item["van"]))
    taken = json.loads(item["taken"] or "[]")
    if taken:
        store.voeg_taken_toe(item_id, item["project"], taken)
        meldingen.append(f"{len(taken)} taak/taken toegevoegd")
    melding = "; ".join(meldingen) or "goedgekeurd zonder concept of taken"
    store.zet_status(item_id, "goedgekeurd", concept=concept, opmerking=melding)
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
