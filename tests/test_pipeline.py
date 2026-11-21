"""Tests zonder echte mailbox en zonder API-aanroepen."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from mailagent import config as config_mod
from mailagent.llm import ClaudeLLM
from mailagent.models import Sortering, Taak, Uitwerking
from mailagent.pipeline import keur_goed, overzicht, run, wijs_af
from mailagent.sources import maak_bronnen
from mailagent.store import Store

ROOT = Path(__file__).resolve().parent.parent


class NepLLM:
    """Sorteert op trefwoorden en houdt bij welke tekst naar 'Claude' zou gaan."""

    def __init__(self):
        self.gezien: list[str] = []

    def sorteer(self, mail, tekst, vast_project):
        self.gezien.append(tekst)
        if vast_project:
            return Sortering(project=vast_project, actie="antwoorden", prioriteit="hoog",
                             deadline=None, samenvatting=mail.onderwerp, reden="test")
        if "review" in mail.onderwerp.lower():
            return Sortering(project="onderzoek", actie="taak", prioriteit="normaal",
                             deadline="2026-11-27", samenvatting="Review-uitnodiging", reden="test")
        return Sortering(project="overig", actie="ter_info", prioriteit="laag",
                         deadline=None, samenvatting=mail.onderwerp, reden="test")

    def werk_uit(self, mail, tekst, sortering):
        self.gezien.append(tekst)
        return Uitwerking(concept_antwoord=f"Concept voor: {mail.onderwerp}",
                          taken=[Taak(omschrijving="Opvolgen", deadline=sortering.deadline)],
                          agenda_voorstel=None, vragen_aan_jou="Akkoord?")


@pytest.fixture
def omgeving(tmp_path):
    shutil.copytree(ROOT / "projecten", tmp_path / "projecten")
    shutil.copytree(ROOT / "tests" / "fixtures", tmp_path / "mail")
    cfg_tekst = (ROOT / "config.example.yaml").read_text()
    cfg_tekst = cfg_tekst.replace(
        """  - type: outlook
    naam: buas
    client_id: "VUL-IN"          # Application (client) ID van je app-registratie
    tenant: organizations
    map: inbox""",
        """  - type: map
    naam: test
    pad: mail""")
    cfg_tekst = cfg_tekst.replace('    # - "@mijnbank.nl"', '    - "@mijnbank.example"')
    cfg_tekst = cfg_tekst.replace("eerste_run_dagen_terug: 2", "eerste_run_dagen_terug: 100000")
    (tmp_path / "config.yaml").write_text(cfg_tekst)
    cfg = config_mod.laad(tmp_path / "config.yaml")
    return cfg, Store(cfg.db_pad), maak_bronnen(cfg)


def test_volledige_run(omgeving):
    cfg, store, bronnen = omgeving
    llm = NepLLM()
    assert run(cfg, store, bronnen, llm) == {"test": 5}

    statussen = {r["onderwerp"]: (r["project"], r["status"]) for r in store.items()}
    assert statussen["Uitstel portfolio stage"] == ("onderwijs", "wacht")          # vaste regel: project
    assert statussen["Agenda board meeting December"] == ("istte", "wacht")        # vaste regel: project
    assert statussen["Invitation to review manuscript TM-2026-0412"] == ("onderzoek", "wacht")
    assert statussen["Nieuwsbrief week 47"] == ("overig", "ter_info")
    assert statussen["Uw rekeningoverzicht"][1] == "handmatig"                     # nooit naar Claude

    alles = "\n".join(llm.gezien)
    assert "123456" not in alles and "[NUMMER]" in alles                          # studentnummer weg
    assert "rekeningoverzicht" not in alles and "NL91ABNA" not in alles           # bankmail nooit verstuurd

    # tweede run: niets nieuws
    assert run(cfg, store, bronnen, llm) == {"test": 0}


def test_goedkeuren_en_afwijzen(omgeving):
    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    wacht = store.items(status="wacht")
    eerste, tweede = wacht[0]["id"], wacht[1]["id"]

    melding = keur_goed(store, bronnen, eerste, "Aangepaste tekst")
    assert "opgeslagen" in melding
    concepten = list((cfg.data_dir / "concepten").glob("*.eml"))
    assert len(concepten) == 1 and "Aangepaste tekst" in concepten[0].read_text()
    assert store.item(eerste)["status"] == "goedgekeurd"
    assert len(store.open_taken()) == 1

    with pytest.raises(ValueError):
        keur_goed(store, bronnen, eerste, "nog een keer")

    wijs_af(store, tweede)
    assert store.item(tweede)["status"] == "afgewezen"

    tekst = overzicht(cfg, store, uren=10**6)
    assert "Open taken" in tekst and "Wacht op jou" in tekst


def test_claude_aanroep_vorm(omgeving):
    """Controleert de request die naar de API gaat, met een nepclient."""
    cfg, store, bronnen = omgeving
    aanroepen = []

    def create(**kw):
        aanroepen.append(kw)
        schema_props = kw["output_config"]["format"]["schema"]["properties"]
        data = ({"project": "onderzoek", "actie": "taak", "prioriteit": "normaal", "deadline": None,
                 "samenvatting": "x", "reden": "y"} if "project" in schema_props else
                {"concept_antwoord": "Hallo", "taken": [], "agenda_voorstel": None, "vragen_aan_jou": None})
        return SimpleNamespace(stop_reason="end_turn", stop_details=None,
                               content=[SimpleNamespace(type="text", text=json.dumps(data))])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    llm = ClaudeLLM(cfg, client=client)
    mail = bronnen["test"].haal_op(None, 10)[1]
    s = llm.sorteer(mail, mail.tekst, None)
    u = llm.werk_uit(mail, mail.tekst, s)
    assert s.project == "onderzoek" and u.concept_antwoord == "Hallo"

    eerste = aanroepen[0]
    assert eerste["model"] == "claude-opus-5-5"
    assert eerste["thinking"] == {"type": "adaptive"}
    assert eerste["output_config"]["effort"] == "low"
    assert eerste["fallbacks"] == "default"
    assert "tools" not in eerste                                   # Claude kan niets uitvoeren
    assert "<email>" in eerste["messages"][0]["content"]
    assert aanroepen[1]["output_config"]["effort"] == "medium"


def test_weigering_wordt_fout_item(omgeving):
    cfg, store, bronnen = omgeving

    def create(**kw):
        return SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category=None), content=[])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    run(cfg, store, bronnen, ClaudeLLM(cfg, client=client))
    fout = store.items(status="fout")
    assert len(fout) == 4 and "weigerde" in fout[0]["opmerking"]
