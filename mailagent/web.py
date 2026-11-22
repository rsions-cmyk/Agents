"""Lokale goedkeuringspagina op http://127.0.0.1:8765. Alleen bereikbaar vanaf je eigen Mac."""

from __future__ import annotations

import html
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlparse

from .config import Config
from .pipeline import keur_goed, overzicht, run, wijs_af
from .store import Store

CSS = """
:root{--bg:#f7f7f5;--kaart:#fff;--tekst:#1d1d1f;--zacht:#6b6b70;--lijn:#e2e2e0;--accent:#2f5d8a;--hoog:#b3261e;--ok:#2e7d32}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--kaart:#202024;--tekst:#ececef;--zacht:#a0a0a8;--lijn:#34343a;--accent:#7fb0e0;--hoog:#ff8a80;--ok:#81c784}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tekst);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px}
nav{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0}nav a{padding:5px 12px;border:1px solid var(--lijn);border-radius:999px;color:var(--tekst);text-decoration:none;font-size:14px}
nav a.actief{background:var(--accent);border-color:var(--accent);color:var(--bg)}
.kaart{background:var(--kaart);border:1px solid var(--lijn);border-radius:10px;padding:14px 16px;margin:10px 0}
.meta{color:var(--zacht);font-size:13px}.prio-hoog{color:var(--hoog);font-weight:600}
.vraag{border-left:3px solid var(--hoog);padding:4px 10px;margin:8px 0}
textarea{width:100%;min-height:160px;font:inherit;padding:8px;border:1px solid var(--lijn);border-radius:6px;background:var(--bg);color:var(--tekst)}
button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--lijn);background:var(--kaart);color:var(--tekst);cursor:pointer}
button.ok{background:var(--ok);border-color:var(--ok);color:var(--bg)}details{margin:6px 0}summary{cursor:pointer;color:var(--zacht);font-size:13px}
pre{white-space:pre-wrap;font:13px/1.45 ui-monospace,Menlo,monospace;background:var(--bg);padding:8px;border-radius:6px;max-height:320px;overflow:auto}
.rij{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}.melding{background:var(--kaart);border:1px solid var(--ok);padding:8px 12px;border-radius:8px}
ul{padding-left:20px}
"""

e = html.escape


def label(project: str) -> str:
    return project.upper() if project == "istte" else project.capitalize()


def maak_handler(cfg: Config, store: Store, bronnen: dict, llm_factory):
    token = secrets.token_urlsafe(24)
    ophalen = {"bezig": False, "laatste": ""}

    def ophalen_op_achtergrond():
        try:
            res = run(cfg, Store(store.pad), bronnen, llm_factory())
            ophalen["laatste"] = ", ".join(f"{k}: {'mislukt' if v < 0 else f'{v} nieuw'}" for k, v in res.items())
        except Exception as ex:
            ophalen["laatste"] = f"ophalen mislukt: {ex}"
        finally:
            ophalen["bezig"] = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # geen mailinhoud in de terminal
            pass

        def _stuur(self, body: str, status: int = 200, soort: str = "text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", soort)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            try:
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser was al weg; de actie zelf is wel uitgevoerd

        def _terug(self, project: str, melding: str = ""):
            self.send_response(303)
            loc = f"/?project={quote(project)}" + (f"&melding={quote(melding)}" if melding else "")
            self.send_header("Location", loc)
            try:
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser was al weg; de actie zelf is wel uitgevoerd

        # --- pagina's ---------------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/overzicht.md":
                return self._stuur(overzicht(cfg, store), soort="text/markdown; charset=utf-8")
            if url.path != "/":
                return self._stuur("Niet gevonden", 404)
            project = q.get("project", ["alles"])[0]
            melding = q.get("melding", [""])[0]
            if ophalen["bezig"]:
                melding = (melding + " · " if melding else "") + "nieuwe mail wordt opgehaald en verwerkt…"
            elif ophalen["laatste"] and not melding:
                melding, ophalen["laatste"] = f"laatste ophaalronde: {ophalen['laatste']}", ""
            self._stuur(self._pagina(project, melding))

        def _pagina(self, project: str, melding: str) -> str:
            p = None if project == "alles" else project
            wacht = store.items(status="wacht", project=p)
            zelf = store.items(status="fout", project=p) + store.items(status="handmatig", project=p)
            taken = store.open_taken(p)
            info = store.items(status="ter_info", project=p)[:30]

            tabs = "".join(
                f'<a href="/?project={x}" class="{"actief" if x == project else ""}">{label(x)}'
                f' ({len(store.items(status="wacht", project=None if x == "alles" else x))})</a>'
                for x in ["alles", *cfg.projecten])
            delen = [f"<h1>Mail-inbox</h1><div class=meta>Niets wordt verstuurd. Goedkeuren zet een concept in je mailbox.</div>",
                     f"<nav>{tabs}</nav>",
                     f'<form method=post action="/ophalen" class=rij><input type=hidden name=t value="{token}">'
                     f'<input type=hidden name=project value="{e(project)}"><button>Nu nieuwe mail ophalen</button>'
                     f'<a class=meta href="/overzicht.md">overzicht als tekst</a></form>']
            if melding:
                delen.append(f"<p class=melding>{e(melding)}</p>")

            delen.append(f"<h2>Wacht op jou ({len(wacht)})</h2>")
            if not wacht:
                delen.append("<p class=meta>Niets te beoordelen.</p>")
            for i in wacht:
                delen.append(self._kaart(store.item(i["id"]), project))

            if zelf:
                delen.append(f"<h2>Zelf bekijken ({len(zelf)})</h2>")
                for i in zelf:
                    delen.append(self._kaart(store.item(i["id"]), project))

            if taken:
                delen.append(f"<h2>Open taken ({len(taken)})</h2>")
                for t in taken:
                    dl = f" <span class=meta>voor {e(t['deadline'])}</span>" if t["deadline"] else ""
                    delen.append(
                        f'<form method=post action="/taak-klaar" class="kaart rij"><input type=hidden name=t value="{token}">'
                        f'<input type=hidden name=id value="{t["id"]}"><input type=hidden name=project value="{e(project)}">'
                        f'<span style="flex:1"><span class=meta>{e(t["project"])}</span> {e(t["omschrijving"])}{dl}</span>'
                        f'<button>Klaar</button></form>')

            if info:
                delen.append(f"<h2>Ter info ({len(info)})</h2><ul>")
                delen += [f"<li>{e(i['samenvatting'] or i['onderwerp'])} <span class=meta>{e(i['van'])}</span></li>" for i in info]
                delen.append("</ul>")

            return (f"<!doctype html><html lang=nl><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                    f"<title>Mail-inbox</title><style>{CSS}</style></head><body><main>{''.join(delen)}</main></body></html>")

        def _kaart(self, i, project: str) -> str:
            prio = f'<span class="prio-{i["prioriteit"]}">{e(i["prioriteit"])}</span>'
            dl = f" · deadline {e(i['deadline'])}" if i["deadline"] else ""
            delen = [f'<div class=kaart><div class=meta>{e(i["project"])} · {e(i["actie"])} · {prio}{dl} · {e(i["datum"][:16])}</div>',
                     f"<strong>{e(i['onderwerp'])}</strong><div class=meta>{e(i['van'])}</div>",
                     f"<p>{e(i['samenvatting'] or '')}</p>"]
            if i["status"] in ("fout", "handmatig"):
                delen.append(f"<p class=vraag>{e(i['opmerking'] or i['reden'] or '')}</p>")
            if i["vragen"]:
                delen.append(f"<div class=vraag><strong>Beslissing voor jou:</strong> {e(i['vragen'])}</div>")
            taken = json.loads(i["taken"] or "[]")
            if taken:
                delen.append("<div class=meta>Taken bij goedkeuren:</div><ul>" +
                             "".join(f"<li>{e(t['omschrijving'])}" + (f" ({e(t['deadline'])})" if t.get("deadline") else "") + "</li>" for t in taken) +
                             "</ul>")
            if i["agenda"]:
                delen.append(f"<div class=meta>Agendavoorstel:</div><p>{e(i['agenda'])}</p>")
            delen.append(f"<details><summary>Oorspronkelijke mail</summary><pre>{e((i['tekst'] or '')[:6000])}</pre></details>")
            delen.append(f'<form method=post action="/beslis"><input type=hidden name=t value="{token}">'
                         f'<input type=hidden name=id value="{i["id"]}"><input type=hidden name=project value="{e(project)}">')
            if i["concept"] or i["actie"] in ("antwoorden", "delegeren") or i["status"] != "wacht":
                delen.append(f'<textarea name=concept placeholder="Concept-antwoord (leeg laten = geen concept)">{e(i["concept"] or "")}</textarea>')
            knop = "Goedkeuren" if i["status"] == "wacht" else "Afgehandeld"
            delen.append(f'<div class=rij><button class=ok name=keuze value=goed>{knop}</button>'
                         '<button name=keuze value=af>Afwijzen</button></div></form></div>')
            return "".join(delen)

        # --- acties -----------------------------------------------------------
        def do_POST(self):
            lengte = int(self.headers.get("Content-Length", 0))
            velden = {k: v[0] for k, v in parse_qs(self.rfile.read(lengte).decode("utf-8")).items()}
            if not secrets.compare_digest(velden.get("t", ""), token):
                return self._stuur("Ongeldig formulier. Herlaad de pagina.", 403)
            project = velden.get("project", "alles")
            pad = urlparse(self.path).path
            try:
                if pad == "/beslis":
                    item_id = int(velden["id"])
                    if velden.get("keuze") == "goed":
                        melding = keur_goed(store, bronnen, item_id, velden.get("concept"))
                    else:
                        wijs_af(store, item_id)
                        melding = "afgewezen"
                elif pad == "/taak-klaar":
                    store.taak_klaar(int(velden["id"]))
                    melding = "taak afgevinkt"
                elif pad == "/ophalen":
                    if ophalen["bezig"]:
                        melding = "ophalen loopt al; ververs de pagina over een paar minuten"
                    else:
                        ophalen["bezig"] = True
                        threading.Thread(target=ophalen_op_achtergrond, daemon=True).start()
                        melding = "ophalen gestart; dit duurt een paar minuten, ververs daarna de pagina"
                else:
                    return self._stuur("Niet gevonden", 404)
            except Exception as ex:
                melding = f"Fout: {ex}"
            self._terug(project, melding)

    return Handler


def serveer(cfg: Config, store: Store, bronnen: dict, llm_factory) -> None:
    server = HTTPServer((cfg.web_host, cfg.web_poort), maak_handler(cfg, store, bronnen, llm_factory))
    print(f"Goedkeuringspagina: http://{cfg.web_host}:{cfg.web_poort}  (stoppen met Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
