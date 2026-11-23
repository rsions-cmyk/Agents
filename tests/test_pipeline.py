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

    def werk_uit(self, mail, tekst, sortering, voorbeelden=None):
        self.gezien.append(tekst)
        self.voorbeelden = voorbeelden
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
    cfg_tekst = cfg_tekst.replace("terugkijken_dagen: 14", "terugkijken_dagen: 100000")
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
    assert store.item(wacht["id"])["status"] == "taak"           # weg uit Beoordelen
    assert store.item(wacht["id"])["concept"]                    # concept bewaard voor Beantwoorden


def test_taak_terugzetten_en_antwoord_vanuit_taak(omgeving):
    from mailagent.pipeline import maak_taak, open_antwoord

    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    info = store.items(status="ter_info")[0]
    maak_taak(store, bronnen, None, info["id"], "Reageren op nieuwsbrief", [], 30, None, None, archiveren=False)
    # mail blijft in de inbox, maar verdwijnt uit Opruimen
    assert store.item(info["id"])["status"] == "taak"
    assert (bronnen["test"].pad / "04-nieuwsbrief.eml").exists()

    taak = store.open_taken()[0]
    store.taak_klaar(taak["id"])
    assert store.open_taken() == [] and store.afgeronde_taken()[0]["afgerond"]
    store.taak_heropen(taak["id"])
    assert store.open_taken()[0]["id"] == taak["id"] and store.afgeronde_taken() == []

    melding = open_antwoord(store, bronnen, info["id"], "Dank, ik lees hem.")
    assert "opgeslagen" in melding and store.item(info["id"])["concept"] == "Dank, ik lees hem."
    assert store.open_taken()[0]["id"] == taak["id"]          # taak blijft staan


def test_inhalen_in_rondes_oudste_eerst(omgeving):
    cfg, store, bronnen = omgeving
    llm = NepLLM()
    # eerste ronde met maximum 2: de twee oudste mails
    assert run(cfg, store, bronnen, llm, maximum=2) == {"test": 2}
    eerste = {r["onderwerp"] for r in store.items()}
    assert eerste == {"Nieuwsbrief week 47", "Uw rekeningoverzicht"}   # 07:00 en 08:00
    # volgende rondes gaan verder waar de vorige stopte, tot alles binnen is
    assert run(cfg, store, bronnen, llm, maximum=2) == {"test": 2}
    assert run(cfg, store, bronnen, llm, maximum=2) == {"test": 1}
    assert run(cfg, store, bronnen, llm, maximum=2) == {"test": 0}
    # een inhaalronde ziet alles opnieuw maar verwerkt niets dubbel
    assert run(cfg, store, bronnen, llm, dagen=100000, maximum=50) == {"test": 0}
    assert len(store.items()) == 5


def test_vlag_bij_taak(omgeving):
    from mailagent.pipeline import maak_taak, markeer_mail

    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    info = store.items(status="ter_info")[0]
    bron = bronnen["test"]
    melding = maak_taak(store, bronnen, None, info["id"], "Lezen", [], 30, None, None, False, vlag=4)
    assert "ingepland" in melding and bron.vlaggen == {"04-nieuwsbrief.eml": 4}
    # tweede taak bij dezelfde mail; eentje afvinken laat de vlag staan
    maak_taak(store, bronnen, None, info["id"], "Delen", [], 15, None, None, False, vlag=4)
    eerste, tweede = store.open_taken()
    store.taak_klaar(eerste["id"])
    markeer_mail(store, bronnen, info["id"], False)
    assert bron.vlaggen                                   # nog een open taak
    store.taak_klaar(tweede["id"])
    markeer_mail(store, bronnen, info["id"], False)
    assert bron.vlaggen == {}
    # zonder vlag-instelling geen markering
    maak_taak(store, bronnen, None, info["id"], "Nog eens", [], 15, None, None, False, vlag=None)
    assert bron.vlaggen == {}


def test_nieuwsbrief_niet_naar_claude(omgeving):
    cfg, store, bronnen = omgeving
    pad = bronnen["test"].pad
    (pad / "08-reclame.eml").write_text(
        "From: Mantel <nieuws@email.mantel.example>\nTo: prive@example.nl\nSubject: Black Friday Sale\n"
        "Date: Fri, 20 Nov 2026 06:30:00 +0100\nMessage-ID: <a8@mantel.example>\n"
        "List-Unsubscribe: <https://mantel.example/afmelden>\n\n50% korting.\n")
    (pad / "09-istte-nieuws.eml").write_text(
        "From: ISTTE <news@istte.org>\nTo: x@buas.nl\nSubject: ISTTE newsletter\n"
        "Date: Fri, 20 Nov 2026 06:40:00 +0100\nMessage-ID: <a9@istte.org>\n"
        "List-Unsubscribe: <https://istte.org/afmelden>\n\nConference news.\n")
    llm = NepLLM()
    run(cfg, store, bronnen, llm)
    items = {r["onderwerp"]: r for r in store.items()}
    assert items["Black Friday Sale"]["status"] == "genegeerd"
    assert not any("korting" in t for t in llm.gezien)                 # niet naar Claude
    assert items["ISTTE newsletter"]["status"] == "wacht"              # eigen regel: wel door


def test_stijlvoorbeelden_en_afzendergedrag(omgeving):
    from datetime import datetime
    from mailagent.models import Email
    from mailagent.pipeline import keur_goed, stijlvoorbeelden, verplaats_items
    from mailagent.web import afzender_voorstellen

    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    wacht = {r["onderwerp"]: r["id"] for r in store.items(status="wacht")}
    keur_goed(store, bronnen, wacht["Agenda board meeting December"], "Dear Chair,\n\nWill do.\n\nWarm regards,\nRob")

    nieuwe = Email(bron="test", bron_id="x", van="Board Chair <chair@istte.org>", aan="", onderwerp="Budget",
                   datum=datetime.now().astimezone(), tekst="Please review the budget.")
    vb = stijlvoorbeelden(cfg, store, nieuwe, "istte")
    assert len(vb) == 1 and "Warm regards" in vb[0]["antwoord"]

    # het voorbeeld gaat mee naar Claude bij een volgende mail van dezelfde afzender
    llm = NepLLM()
    (bronnen["test"].pad / "10-chair.eml").write_text(
        "From: Board Chair <chair@istte.org>\nTo: x@buas.nl\nSubject: Budget 2027\n"
        "Date: Fri, 20 Nov 2026 15:00:00 +0100\nMessage-ID: <a10@istte.org>\n\nPlease review.\n")
    run(cfg, store, bronnen, llm)
    assert llm.voorbeelden and "Warm regards" in llm.voorbeelden[0]["antwoord"]

    # nooit-naar-Claude-afzenders gaan niet mee als voorbeeld
    bank = store.items(status="handmatig")[0]
    keur_goed(store, bronnen, bank["id"], "Geheim antwoord aan de bank")
    bank_mail = Email(bron="test", bron_id="y", van="Mijn Bank <service@mijnbank.example>", aan="",
                      onderwerp="x", datum=datetime.now().astimezone(), tekst="")
    assert all("Geheim" not in v["antwoord"] for v in stijlvoorbeelden(cfg, store, bank_mail, "overig"))

    # afzendergedrag en voorstel na drie keer weggooien
    for i in range(3):
        (bronnen["test"].pad / f"2{i}-promo.eml").write_text(
            f"From: Winkel <promo@winkel.example>\nTo: x@buas.nl\nSubject: Aanbieding {i}\n"
            f"Date: Fri, 20 Nov 2026 0{i+1}:00:00 +0100\nMessage-ID: <p{i}@winkel.example>\n\nKoop nu.\n")
    run(cfg, store, bronnen, NepLLM())
    promo = [r["id"] for r in store.items(status="ter_info") if "Aanbieding" in r["onderwerp"]]
    verplaats_items(store, bronnen, promo[:2], "prullenbak")
    assert store.afzender_gedrag()["promo@winkel.example"]["prullenbak"] == 2
    assert afzender_voorstellen(store)["voorstellen"] == []
    assert afzender_voorstellen(store)["gedrag"]["promo@winkel.example"]["prullenbak"] == 2
    verplaats_items(store, bronnen, promo[2:], "prullenbak")
    assert afzender_voorstellen(store)["voorstellen"][0]["adres"] == "promo@winkel.example"


def test_taken_aanpassen_bij_goedkeuren(omgeving):
    from mailagent.pipeline import keur_goed

    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    a, b = [r["id"] for r in store.items(status="wacht")][:2]
    keur_goed(store, bronnen, a, "", taken=[{"omschrijving": "Eigen taak", "deadline": "2026-12-01"},
                                           {"omschrijving": "   ", "deadline": ""}])
    taken = store.open_taken()
    assert [(t["omschrijving"], t["deadline"]) for t in taken] == [("Eigen taak", "2026-12-01")]
    keur_goed(store, bronnen, b, "", taken=[])                  # alles weggehaald: geen taken
    assert len(store.open_taken()) == 1


def test_opbergen_in_map_met_voorstel(omgeving):
    from mailagent.pipeline import map_voorstellen, verplaats_items

    cfg, store, bronnen = omgeving
    pad = bronnen["test"].pad
    (pad / "12-enter.eml").write_text(
        "From: ENTER27 <enter27@easychair.example>\nTo: x@buas.nl\nSubject: ENTER27 Doctoral workshop - Submission Outcome\n"
        "Date: Fri, 20 Nov 2026 16:00:00 +0100\nMessage-ID: <e1@easychair.example>\n\nAccepted.\n")
    run(cfg, store, bronnen, NepLLM())
    it = next(r for r in store.items() if r["onderwerp"].startswith("ENTER27"))
    mappen = bronnen["test"].mappenlijst()

    # zonder geschiedenis: voorstel op basis van de mapnaam
    vs = map_voorstellen(store, store.item(it["id"]), mappen)
    assert vs[0]["map"] == "Projecten/ENTER÷IFITT" and "onderwerp" in vs[0]["reden"]

    melding = verplaats_items(store, bronnen, [it["id"]], "map:Projecten/ENTER÷IFITT")
    assert "map Projecten/ENTER÷IFITT" in melding
    assert store.item(it["id"])["status"] == "opgeborgen"
    assert (pad / "_map_Projecten_ENTER÷IFITT" / "12-enter.eml").exists()

    # volgende mail van dezelfde afzender: voorstel op basis van eerdere keuze
    (pad / "13-enter.eml").write_text(
        "From: ENTER27 <enter27@easychair.example>\nTo: x@buas.nl\nSubject: Registration reminder\n"
        "Date: Fri, 20 Nov 2026 17:00:00 +0100\nMessage-ID: <e2@easychair.example>\n\nRegister.\n")
    run(cfg, store, bronnen, NepLLM())
    it2 = next(r for r in store.items() if r["onderwerp"] == "Registration reminder")
    vs2 = map_voorstellen(store, store.item(it2["id"]), mappen)
    assert vs2[0]["map"] == "Projecten/ENTER÷IFITT" and "afzender" in vs2[0]["reden"]

    with pytest.raises(ValueError):
        verplaats_items(store, bronnen, [it2["id"]], "map:")


def test_ongedaan_maken_tijdfactor_en_eerste_stap(omgeving):
    from datetime import datetime
    from mailagent.pipeline import afspraak_titel, maak_taak, verplaats_items, zet_terug

    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    info = store.items(status="ter_info")[0]
    pad = bronnen["test"].pad

    # archiveren en ongedaan maken: mail terug, item terug in Opruimen
    verplaats_items(store, bronnen, [info["id"]], "archief")
    assert not (pad / "04-nieuwsbrief.eml").exists()
    assert "1 mail(s) terug" in zet_terug(store, bronnen, [info["id"]])
    assert (pad / "04-nieuwsbrief.eml").exists() and store.item(info["id"])["status"] == "ter_info"
    # ook vanuit een map
    verplaats_items(store, bronnen, [info["id"]], "map:ISTTE")
    zet_terug(store, bronnen, [info["id"]])
    assert store.item(info["id"])["status"] == "ter_info"
    assert store.vandaag_afgehandeld() == 0          # terugzetten telt niet als afgehandeld

    # eerste stap in de titel van de afspraak
    assert afspraak_titel("Mentoren benaderen", ["Lijst van vijf mentoren openen", "Mails sturen"]) == \
        "Mentoren benaderen · begin met: lijst van vijf mentoren openen"
    assert afspraak_titel("Lezen", []) == "Lezen"

    # tijdfactor uit oordelen
    for oordeel in (1, 1, 1, 0):
        tid = store.nieuwe_taak(None, "onderwijs", "x", None, 60, [], None)
        store.taak_klaar(tid); store.taak_oordeel(tid, oordeel)
    factor, n = store.tijdfactor("onderwijs")
    assert n == 4 and abs(factor - (1.4 * 3 + 1.0) / 4) < 1e-9
    assert store.tijdfactor("istte") == (1.0, 0)


def test_opgemaakte_weergave_veilig():
    from mailagent.opmaak import html_uit_bron, veilig

    bron = ("From: a@b.nl\nSubject: x\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary=XX\n\n"
            "--XX\nContent-Type: text/plain; charset=utf-8\n\nHoi Rob,\n--XX\nContent-Type: text/html; charset=utf-8\n\n"
            "<html><head><script>alert(1)</script></head><body onload=\"steel()\"><p>Hoi Rob,</p>"
            "<p style=\"color:#c00\">Antwoord tussen de regels</p><img src=\"https://tracker.example/p.gif\">"
            "<a href=\"javascript:alert(2)\">x</a><iframe src=\"https://x\"></iframe><form action=\"/\"><input></form></body></html>\n--XX--\n")
    h = veilig(html_uit_bron(bron))
    assert "Antwoord tussen de regels" in h and "color:#c00" in h
    assert "<script" not in h.lower() and "alert(1)" not in h and "onload" not in h
    assert "javascript:" not in h and "<iframe" not in h and "<form" not in h
    assert "default-src 'none'" in h and "tracker.example" not in h   # externe afbeelding komt niet eens in de pagina


def test_verzenden_alleen_via_bevestigde_klik_en_gedaan(omgeving):
    import inspect
    from pathlib import Path
    from mailagent import pipeline
    from mailagent.pipeline import keur_goed, markeer_gedaan, zet_gedaan_terug

    # bewaking: automatische rondes kunnen niet versturen, en er is precies één verzendopdracht in de code
    assert "verzend" not in inspect.getsource(pipeline.run) and "verzend" not in inspect.getsource(pipeline.verwerk_mail)
    bronnen_code = (ROOT / "mailagent" / "sources" / "applemail.py").read_text()
    assert bronnen_code.count("send om") == 1
    alle_code = "".join(p.read_text() for p in (ROOT / "mailagent").rglob("*.py"))
    assert "smtplib" not in alle_code and "/sendMail" not in alle_code and "Mail.Send\"" not in alle_code

    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    a, b = [r["id"] for r in store.items(status="wacht")][:2]
    with pytest.raises(ValueError):
        keur_goed(store, bronnen, a, "  ", verzenden=True)          # geen tekst, niets versturen
    assert "verzonden aan" in keur_goed(store, bronnen, a, "Dank, akkoord.", verzenden=True)
    assert store.item(a)["status"] == "verzonden"
    assert (cfg.data_dir / "verzonden").exists()

    markeer_gedaan(store, b)
    assert store.item(b)["status"] == "gedaan"
    zet_gedaan_terug(store, b)
    assert store.item(b)["status"] == "wacht"


def test_mapvoorstel_op_projectnaam(omgeving):
    from mailagent.pipeline import map_voorstellen
    cfg, store, bronnen = omgeving
    run(cfg, store, bronnen, NepLLM())
    it = next(r for r in store.items() if r["project"] == "istte")
    vs = map_voorstellen(store, store.item(it["id"]), bronnen["test"].mappenlijst())
    assert vs[0] == {"map": "ISTTE", "reden": "zelfde naam als het project"}
