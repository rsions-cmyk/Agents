"""Lokale goedkeuringsapp op http://127.0.0.1:8765. Alleen bereikbaar vanaf je eigen Mac.

De pagina (static/app.html) haalt haar gegevens op via /api/data en voert acties uit via
POST /api/<actie>. Elke POST moet de sessiesleutel in de header X-Token meesturen. Een andere
website kan die header niet meesturen, dus alleen deze pagina kan acties uitvoeren.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .pipeline import afzender_adres, keur_goed, overzicht, run, verplaats_items, wijs_af
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


def verzamel(cfg: Config, store: Store, ophalen: dict) -> dict:
    tellingen = {p: len(store.items(status="wacht", project=p)) for p in cfg.projecten}
    zelf = store.items(status="fout") + store.items(status="handmatig")
    return {
        "projecten": [{"naam": p, "wacht": tellingen[p]} for p in cfg.projecten],
        "wacht": [item_dict(store.item(r["id"]), True) for r in store.items(status="wacht")],
        "zelf": [item_dict(store.item(r["id"]), True) for r in zelf],
        "info": [item_dict(r) for r in store.items(status="ter_info")[:100]],
        "overgeslagen": [item_dict(r) for r in store.items(status="genegeerd")[:100]],
        "taken": [dict(t) for t in store.open_taken()],
        "regels": [dict(r) for r in store.afzender_regels()],
        "ophalen": {"bezig": ophalen["bezig"], "laatste": ophalen["laatste"]},
    }


def maak_handler(cfg: Config, store: Store, bronnen: dict, llm_factory):
    token = secrets.token_urlsafe(24)
    db_pad = store.pad
    ophalen = {"bezig": False, "laatste": ""}
    mail_slot = threading.Lock()  # Apple Mail krijgt één opdracht tegelijk

    def ophalen_op_achtergrond():
        try:
            res = run(cfg, Store(db_pad), bronnen, llm_factory())
            ophalen["laatste"] = ", ".join(f"{k}: {'mislukt' if v < 0 else f'{v} nieuw'}" for k, v in res.items())
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
                        melding = keur_goed(store, bronnen, int(v["id"]), v.get("concept"))
                elif actie == "afwijzen":
                    wijs_af(store, int(v["id"]))
                    melding = "Afgewezen"
                elif actie == "taak-klaar":
                    store.taak_klaar(int(v["id"]))
                    melding = "Taak afgevinkt"
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
