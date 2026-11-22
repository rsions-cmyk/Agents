"""Lokale goedkeuringsapp op http://127.0.0.1:8765. Alleen bereikbaar vanaf je eigen Mac.

De pagina (static/app.html) haalt haar gegevens op via /api/data en voert acties uit via
POST /api/<actie>. Elke POST moet de sessiesleutel in de header X-Token meesturen. Een andere
website kan die header niet meesturen, dus alleen deze pagina kan acties uitvoeren.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from datetime import date, datetime, timedelta

from .agenda import lees_werktijd, maak_agenda, vrije_momenten
from .config import anonimiseer, mag_naar_claude
from .models import Email
from .models import Sortering
from .pipeline import afzender_adres, stijlvoorbeelden, keur_goed, maak_taak, markeer_mail, open_antwoord, overzicht, run, verplaats_items, wijs_af
from .store import Store

APP_HTML = Path(__file__).with_name("static") / "app.html"


def item_dict(rij, volledig: bool = False) -> dict:
    d = {k: rij[k] for k in ("id", "project", "actie", "prioriteit", "deadline", "samenvatting",
                             "status", "van", "onderwerp", "datum")}
    if volledig:
        d.update({
            "reden": rij["reden"], "concept": rij["concept"] or "", "agenda": rij["agenda"],
            "vragen": rij["vragen"], "opmerking": rij["opmerking"],
            "taken": json.loads(rij["taken"] or "[]"), "tekst": (rij["tekst"] or "")[:8000],
        })
    return d


def taak_dict(store: Store, t, mail: bool = True) -> dict:
    d = {**dict(t), "stappen": json.loads(t["stappen"] or "[]"), "mail": None}
    if mail and t["item_id"]:
        it = store.item(t["item_id"])
        if it is not None:
            d["mail"] = {"id": it["id"], "van": it["van"], "onderwerp": it["onderwerp"], "datum": it["datum"],
                         "project": it["project"], "concept": it["concept"] or "", "tekst": (it["tekst"] or "")[:8000],
                         "kan_openen": bool(it["thread_id"])}
    return d


def afzender_voorstellen(store: Store) -> dict:
    """Wat de eigenaar eerder met mail van elke afzender deed, en welke afzenders hij steeds weggooit."""
    gedrag = store.afzender_gedrag()
    overgeslagen = {r["adres"] for r in store.afzender_regels()}
    namen = {}
    for r in store.items(status="ter_info") + store.items(status="genegeerd"):
        namen.setdefault(afzender_adres(r["van"] or ""), r["van"])
    voorstellen = [{"adres": a, "van": namen.get(a, a), "weg": g.get("prullenbak", 0)}
                   for a, g in gedrag.items()
                   if g.get("prullenbak", 0) >= 3 and not g.get("antwoord") and not g.get("taak") and a not in overgeslagen]
    voorstellen.sort(key=lambda v: -v["weg"])
    return {"gedrag": {a: g for a, g in gedrag.items() if a in namen}, "voorstellen": voorstellen[:8]}


def verzamel(cfg: Config, store: Store, ophalen: dict) -> dict:
    tellingen = {p: len(store.items(status="wacht", project=p)) for p in cfg.projecten}
    zelf = store.items(status="fout") + store.items(status="handmatig")
    return {
        "projecten": [{"naam": p, "wacht": tellingen[p]} for p in cfg.projecten],
        "wacht": [item_dict(store.item(r["id"]), True) for r in store.items(status="wacht")],
        "zelf": [item_dict(store.item(r["id"]), True) for r in zelf],
        "info": [item_dict(r) for r in store.items(status="ter_info")[:100]],
        "overgeslagen": [item_dict(r) for r in store.items(status="genegeerd")[:100]],
        "taken": [taak_dict(store, t) for t in store.open_taken()],
        "afgerond": [taak_dict(store, t, mail=False) for t in store.afgeronde_taken()],
        "regels": [dict(r) for r in store.afzender_regels()],
        **afzender_voorstellen(store),
        "ophalen": {"bezig": ophalen["bezig"], "laatste": ophalen["laatste"]},
    }


def maak_handler(cfg: Config, store: Store, bronnen: dict, llm_factory):
    token = secrets.token_urlsafe(24)
    db_pad = store.pad
    ophalen = {"bezig": False, "laatste": ""}
    mail_slot = threading.Lock()  # Apple Mail krijgt één opdracht tegelijk
    agenda_cache: dict = {}

    def agenda():
        """Agenda pas koppelen als hij nodig is; een fout wordt onthouden tot de volgende poging."""
        if "agenda" not in agenda_cache or agenda_cache["agenda"] is None:
            agenda_cache["agenda"], agenda_cache["fout"] = maak_agenda(cfg)
        return agenda_cache["agenda"], agenda_cache["fout"]

    def taakvoorstel(store: Store, item_id: int) -> dict:
        it = store.item(item_id)
        if it is None:
            raise ValueError("onbekende mail")
        mail = Email(bron=it["bron"], bron_id=it["bron_id"], van=it["van"] or "", aan=it["aan"] or "",
                     onderwerp=it["onderwerp"] or "", datum=datetime.fromisoformat(it["datum"]), tekst=it["tekst"] or "")
        if mag_naar_claude(cfg, mail):
            plan = llm_factory().plan_taak(mail, anonimiseer(cfg, mail.tekst), it["project"])
            voorstel = plan.model_dump()
        else:
            voorstel = {"titel": f"Afhandelen: {mail.onderwerp}", "stappen": [], "minuten": 30,
                        "deadline": it["deadline"], "toelichting": "Deze afzender gaat niet naar Claude; standaard 30 minuten."}
        voorstel["deadline"] = voorstel["deadline"] or it["deadline"]
        ag, fout = agenda()
        voorstel["momenten"], voorstel["agenda_fout"] = [], fout
        werk = lees_werktijd(cfg.agenda)
        voorstel["werktijd"] = {"begin": werk.begin.strftime("%H:%M"), "eind": werk.eind.strftime("%H:%M"),
                                "dagen": sorted(werk.dagen)}
        if ag is not None:
            nu = datetime.now().astimezone()
            try:
                dl = date.fromisoformat(voorstel["deadline"]) if voorstel["deadline"] else None
            except ValueError:
                dl = None
            try:
                bezet = ag.bezet(nu, nu + timedelta(days=werk.dagen_vooruit + 1))
                momenten = vrije_momenten(bezet, voorstel["minuten"], nu, werk, dl)
                if not momenten and dl:  # vóór de deadline is niets vrij: kijk erna
                    momenten = vrije_momenten(bezet, voorstel["minuten"], nu, werk, None)
                voorstel["momenten"] = [m.isoformat(timespec="minutes") for m in momenten]
            except Exception as ex:
                voorstel["agenda_fout"] = f"Agenda niet te lezen: {ex}"
        return voorstel

    def ophalen_op_achtergrond():
        try:
            rest: dict = {}
            res = run(cfg, Store(db_pad), bronnen, llm_factory(), voortgang=rest)
            ophalen["laatste"] = ", ".join(
                f"{k}: mislukt" if v < 0 else f"{k}: {v} nieuw" + (f", nog {rest[k]} te gaan" if rest.get(k) else "")
                for k, v in res.items())
        except Exception as ex:
            ophalen["laatste"] = f"ophalen mislukt: {ex}"
        finally:
            ophalen["bezig"] = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # geen mailinhoud in de terminal
            pass

        def _stuur(self, body: str | bytes, status: int = 200, soort: str = "text/html; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", soort)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            try:
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser was al weg; de actie zelf is wel uitgevoerd

        def _json(self, obj: dict, status: int = 200):
            self._stuur(json.dumps(obj, ensure_ascii=False), status, "application/json; charset=utf-8")

        def do_GET(self):
            store = Store(db_pad)  # eigen verbinding per verzoek: verzoeken lopen naast elkaar
            pad = urlparse(self.path).path
            if pad == "/":
                html = APP_HTML.read_text(encoding="utf-8").replace("__TOKEN__", token)
                return self._stuur(html)
            if pad == "/api/data":
                return self._json(verzamel(cfg, store, ophalen))
            if pad == "/overzicht.md":
                return self._stuur(overzicht(cfg, store), soort="text/markdown; charset=utf-8")
            self._stuur("Niet gevonden", 404, "text/plain; charset=utf-8")

        def do_POST(self):
            if not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                return self._json({"ok": False, "melding": "Ongeldige sessie. Herlaad de pagina."}, 403)
            lengte = int(self.headers.get("Content-Length", 0) or 0)
            try:
                v = json.loads(self.rfile.read(lengte) or b"{}")
            except json.JSONDecodeError:
                return self._json({"ok": False, "melding": "Ongeldig verzoek"}, 400)
            actie = urlparse(self.path).path.removeprefix("/api/")
            store = Store(db_pad)
            try:
                if actie == "goedkeuren":
                    with mail_slot:
                        melding = keur_goed(store, bronnen, int(v["id"]), v.get("concept"), v.get("taken"))
                elif actie == "afwijzen":
                    wijs_af(store, int(v["id"]))
                    melding = "Afgewezen"
                elif actie in ("taak-klaar", "taak-heropen"):
                    klaar = actie == "taak-klaar"
                    (store.taak_klaar if klaar else store.taak_heropen)(int(v["id"]))
                    melding = "Taak afgevinkt" if klaar else "Taak teruggezet"
                    t = store.taak(int(v["id"]))
                    if cfg.vlag_aan and t is not None and t["item_id"]:
                        def vlag(item_id=t["item_id"], aan=not klaar):
                            with mail_slot:
                                markeer_mail(Store(db_pad), bronnen, item_id, aan, cfg.vlag_kleur)
                        threading.Thread(target=vlag, daemon=True).start()   # niet wachten op Apple Mail
                elif actie == "mail-openen":
                    it = store.item(int(v["id"]))
                    mid = (it["thread_id"] or "").strip().strip("<>") if it else ""
                    if not mid:
                        return self._json({"ok": False, "melding": "Deze mail kan niet in Mail worden geopend"})
                    subprocess.run(["open", f"message://%3c{mid}%3e"], check=False, timeout=20)
                    melding = ""
                elif actie == "antwoord-concept":
                    it = store.item(int(v["id"]))
                    mail = Email(bron=it["bron"], bron_id=it["bron_id"], van=it["van"] or "", aan=it["aan"] or "",
                                 onderwerp=it["onderwerp"] or "", datum=datetime.fromisoformat(it["datum"]), tekst=it["tekst"] or "")
                    if not mag_naar_claude(cfg, mail):
                        return self._json({"ok": False, "melding": "Deze afzender gaat niet naar Claude; schrijf het antwoord zelf"})
                    s = Sortering(project=it["project"], actie="antwoorden", prioriteit=it["prioriteit"],
                                  deadline=it["deadline"], samenvatting=it["samenvatting"] or it["onderwerp"], reden="")
                    u = llm_factory().werk_uit(mail, anonimiseer(cfg, mail.tekst), s,
                                               stijlvoorbeelden(cfg, store, mail, it["project"]))
                    return self._json({"ok": True, "concept": u.concept_antwoord or "", "vragen": u.vragen_aan_jou})
                elif actie == "antwoord-openen":
                    with mail_slot:
                        melding = open_antwoord(store, bronnen, int(v["id"]), v.get("tekst", ""))
                elif actie == "verplaats":
                    ids = [int(x) for x in v.get("ids", [])]
                    if not ids:
                        return self._json({"ok": False, "melding": "Niets geselecteerd"})
                    if v.get("doel") == "overslaan":
                        adressen = set()
                        for i in ids:
                            it = store.item(i)
                            if it:
                                adres = afzender_adres(it["van"])
                                adressen.add(adres)
                                store.zet_afzender_regel(adres, "negeren", it["project"])
                        melding = f"{len(adressen)} afzender(s) worden voortaan overgeslagen"
                    else:
                        with mail_slot:
                            melding = verplaats_items(store, bronnen, ids, v.get("doel", ""))
                        if "niet gelukt" in melding:
                            return self._json({"ok": False, "melding": melding})
                elif actie == "taak-voorstel":
                    return self._json({"ok": True, "voorstel": taakvoorstel(store, int(v["id"]))})
                elif actie == "dag":
                    ag, fout = agenda()
                    if ag is None:
                        return self._json({"ok": False, "melding": fout})
                    dag = date.fromisoformat(v["datum"])
                    van = datetime.combine(dag, datetime.min.time()).astimezone()
                    tot = van + timedelta(days=1)
                    if hasattr(ag, "afspraken"):
                        lijst = ag.afspraken(van, tot)
                    else:
                        lijst = [{"titel": "Bezet", "start": b, "eind": e, "heledag": False, "vrij": False, "kalender": ""}
                                 for b, e in ag.bezet(van, tot)]
                    return self._json({"ok": True, "afspraken": [
                        {**a, "start": a["start"].isoformat(timespec="minutes"), "eind": a["eind"].isoformat(timespec="minutes")}
                        for a in lijst]})
                elif actie == "taak-maak":
                    start = datetime.fromisoformat(v["start"]).astimezone() if v.get("start") else None
                    stappen = [s.strip() for s in v.get("stappen", []) if s.strip()]
                    ag = agenda()[0] if start else None
                    with mail_slot:
                        melding = maak_taak(store, bronnen, ag, int(v["id"]), v["titel"].strip() or "Taak",
                                            stappen, int(v.get("minuten") or 30), v.get("deadline") or None,
                                            start, bool(v.get("archiveren")),
                                            vlag=cfg.vlag_kleur if cfg.vlag_aan else None)
                elif actie == "regels-toevoegen":
                    adressen = [a.strip().lower() for a in v.get("adressen", []) if a.strip()]
                    for a in adressen:
                        store.zet_afzender_regel(a, "negeren", None)
                    melding = f"{len(adressen)} afzender(s) worden voortaan overgeslagen"
                elif actie == "regel-weg":
                    store.verwijder_afzender_regel(v["adres"])
                    melding = f"{v['adres']} wordt niet meer overgeslagen"
                elif actie == "ophalen":
                    if ophalen["bezig"]:
                        melding = "Ophalen loopt al"
                    else:
                        ophalen["bezig"], ophalen["laatste"] = True, ""
                        threading.Thread(target=ophalen_op_achtergrond, daemon=True).start()
                        melding = "Nieuwe mail wordt opgehaald"
                else:
                    return self._json({"ok": False, "melding": "Onbekende actie"}, 404)
            except Exception as ex:
                return self._json({"ok": False, "melding": f"Fout: {ex}"})
            self._json({"ok": True, "melding": melding})

    return Handler


def serveer(cfg: Config, store: Store, bronnen: dict, llm_factory) -> None:
    server = ThreadingHTTPServer((cfg.web_host, cfg.web_poort), maak_handler(cfg, store, bronnen, llm_factory))
    print(f"Goedkeuringsapp: http://{cfg.web_host}:{cfg.web_poort}  (stoppen met Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
