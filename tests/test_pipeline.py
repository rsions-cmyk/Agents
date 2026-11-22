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

    def plan_taak(self, mail, tekst, project):
        from mailagent.models import Taakplan
        self.gezien.append(tekst)
        return Taakplan(titel=f"Afhandelen: {mail.onderwerp}", stappen=["Lezen", "Reageren"], minuten=45,
                        deadline=None, toelichting="Korte mail, kort antwoord.")


@pytest.fixture
def omgeving(tmp_path):
    shutil.copytree(ROOT / "projecten", tmp_path / "projecten")
    shutil.copytree(ROOT / "tests" / "fixtures", tmp_path / "mail")
    cfg_tekst = (ROOT / "config.example.yaml").read_text()
    cfg_tekst = cfg_tekst.replace(
        """  - type: applemail
    naam: buas
    account: "Exchange"          # naam van het account zoals Apple Mail het toont
    mailbox: "Inbox"
    archief: "Archive"           # waar 'Archiveren' de mail heen verplaatst
    prullenbak: "Deleted Items"  # waar 'Naar prullenbak' de mail heen verplaatst\n""",
        """  - type: map
    naam: test
    pad: mail\n""")
    begin = cfg_tekst.index("  # Google-account in Apple Mail")
    eind = cfg_tekst.index("  # - type: outlook")
    cfg_tekst = cfg_tekst[:begin] + cfg_tekst[eind:]
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


def test_claude_code_aanroep(omgeving):
    """Controleert de `claude -p`-aanroep met een nep-subprocess."""
    from mailagent.llm import ClaudeCodeLLM, LLMFout

    cfg, store, bronnen = omgeving
    aanroepen = []

    def nep_run(cmd, **kw):
        aanroepen.append((cmd, kw))
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        data = ({"project": "onderzoek", "actie": "antwoorden", "prioriteit": "hoog", "deadline": None,
                 "samenvatting": "x", "reden": "y"} if "project" in schema["properties"] else
                {"concept_antwoord": "Dear Tracy", "taken": [], "agenda_voorstel": None, "vragen_aan_jou": None})
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=json.dumps({"result": "", "is_error": False, "structured_output": data}))

    llm = ClaudeCodeLLM(cfg, uitvoerder=nep_run)
    mail = bronnen["test"].haal_op(None, 10)[1]
    s = llm.sorteer(mail, mail.tekst, None)
    u = llm.werk_uit(mail, mail.tekst, s)
    assert u.concept_antwoord == "Dear Tracy"

    cmd, kw = aanroepen[0]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""                       # geen tools
    assert "mcp__*" in cmd and "--strict-mcp-config" in cmd           # geen MCP-servers
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert mail.onderwerp in kw["input"] and mail.onderwerp not in " ".join(cmd)  # mail via stdin
    assert "ANTHROPIC_API_KEY" not in kw["env"]                       # abonnement, geen API-sleutel

    def kapot(cmd, **kw):
        return SimpleNamespace(returncode=1, stderr="", stdout=json.dumps({"is_error": True, "result": "Not logged in"}))

    with pytest.raises(LLMFout, match="Not logged in"):
        ClaudeCodeLLM(cfg, uitvoerder=kapot).sorteer(mail, mail.tekst, None)


def test_archiveren_verwijderen_en_overslaan(omgeving):
    from mailagent.pipeline import verplaats_items, afzender_adres

    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    info = store.items(status="ter_info")
    assert len(info) == 1                                    # nieuwsbrief

    melding = verplaats_items(store, bronnen, [info[0]["id"]], "archief")
    assert melding.startswith("1 mail(s) verplaatst naar het archief")
    assert store.item(info[0]["id"])["status"] == "gearchiveerd"
    assert (bronnen["test"].pad / "_archief" / "04-nieuwsbrief.eml").exists()
    # nog een keer verplaatsen doet niets
    assert verplaats_items(store, bronnen, [info[0]["id"]], "prullenbak").startswith("0 mail(s)")

    # afzender voortaan overslaan: volgende mail van die afzender gaat niet naar Claude
    store.zet_afzender_regel(afzender_adres("BUas Nieuws <nieuwsbrief@buas.nl>"), "negeren", "overig")
    (bronnen["test"].pad / "06-nieuwsbrief.eml").write_text(
        "From: BUas Nieuws <nieuwsbrief@buas.nl>\nTo: x@buas.nl\nSubject: Nieuwsbrief week 48\n"
        "Date: Fri, 27 Nov 2026 07:00:00 +0100\nMessage-ID: <a6@buas.nl>\n\nKerstborrel.\n")
    llm = NepLLM()
    run(cfg, store, bronnen, llm)
    nieuw = [r for r in store.items(status="genegeerd") if r["onderwerp"] == "Nieuwsbrief week 48"]
    assert len(nieuw) == 1 and llm.gezien == []              # Claude niet aangeroepen

    with pytest.raises(ValueError):
        verplaats_items(store, bronnen, [1], "definitief")


def test_web_api(omgeving):
    """De app levert gegevens en weigert acties zonder sessiesleutel."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import HTTPServer

    from mailagent.web import maak_handler

    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    server = HTTPServer(("127.0.0.1", 0), maak_handler(cfg, store, bronnen, NepLLM))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    basis = f"http://127.0.0.1:{server.server_port}"
    try:
        pagina = urllib.request.urlopen(basis + "/").read().decode()
        token = pagina.split('const TOKEN = "')[1].split('"')[0]
        data = json.loads(urllib.request.urlopen(basis + "/api/data").read())
        assert len(data["wacht"]) == 3 and data["wacht"][0]["concept"]

        def post(actie, body, sleutel):
            req = urllib.request.Request(basis + "/api/" + actie, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Token": sleutel})
            try:
                return json.loads(urllib.request.urlopen(req).read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())

        assert post("afwijzen", {"id": data["wacht"][0]["id"]}, "verkeerd")["ok"] is False
        assert store.item(data["wacht"][0]["id"])["status"] == "wacht"
        assert post("afwijzen", {"id": data["wacht"][0]["id"]}, token)["ok"] is True
        assert store.item(data["wacht"][0]["id"])["status"] == "afgewezen"
    finally:
        server.shutdown()


def test_vrije_momenten():
    from datetime import datetime, timedelta, timezone, date
    from mailagent.agenda import Werktijd, vrije_momenten

    tz = timezone(timedelta(hours=1))
    nu = datetime(2026, 11, 23, 8, 10, tzinfo=tz)          # maandag 08:10
    werk = Werktijd()
    bezet = [(datetime(2026, 11, 23, 9, 0, tzinfo=tz), datetime(2026, 11, 23, 12, 0, tzinfo=tz)),
             (datetime(2026, 11, 24, 0, 0, tzinfo=tz), datetime(2026, 11, 24, 23, 59, tzinfo=tz))]
    m = vrije_momenten(bezet, 60, nu, werk)
    assert m[0] == datetime(2026, 11, 23, 12, 0, tzinfo=tz)  # na het blok van 9 tot 12
    assert m[1].date() == date(2026, 11, 25)                  # dinsdag is vol
    assert len(m) == 3 and len({x.date() for x in m}) == 3    # één per dag
    # deadline begrenst
    assert vrije_momenten(bezet, 60, nu, werk, deadline=date(2026, 11, 24)) == [m[0]]
    # weekend overslaan
    zat = datetime(2026, 11, 28, 10, 0, tzinfo=tz)
    assert vrije_momenten([], 30, zat, werk)[0] == datetime(2026, 11, 30, 9, 0, tzinfo=tz)


def test_taak_maken_en_inplannen(omgeving):
    from datetime import datetime, timedelta
    from mailagent.models import Taakplan
    from mailagent.pipeline import maak_taak

    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    info = store.items(status="ter_info")[0]

    class NepAgenda:
        def __init__(self):
            self.afspraken = []
        def bezet(self, van, tot):
            return []
        def maak_afspraak(self, titel, start, eind, notities):
            self.afspraken.append((titel, start, eind, notities))
            return "ingepland"

    ag = NepAgenda()
    start = datetime(2026, 11, 25, 10, 0).astimezone()
    melding = maak_taak(store, bronnen, ag, info["id"], "Nieuwsbrief lezen", ["Lezen", "Delen met team"],
                        45, "2026-11-27", start, archiveren=True)
    assert "Taak gemaakt" in melding and "ingepland" in melding and "archief" in melding
    titel, b, e, notities = ag.afspraken[0]
    assert e - b == timedelta(minutes=45) and "- Delen met team" in notities
    taak = store.open_taken()[0]
    assert taak["minuten"] == 45 and json.loads(taak["stappen"]) == ["Lezen", "Delen met team"] and taak["gepland"]
    assert store.item(info["id"])["status"] == "gearchiveerd"

    # zonder agenda kan er niet worden ingepland
    wacht = store.items(status="wacht")[0]
    with pytest.raises(ValueError):
        maak_taak(store, bronnen, None, wacht["id"], "x", [], 30, None, start, False)
    # zonder moment wel; een wachtend item blijft wachten
    maak_taak(store, bronnen, None, wacht["id"], "Reageren", [], 30, None, None, False)
    assert store.item(wacht["id"])["status"] == "wacht"
