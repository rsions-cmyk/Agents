"""Proefserver: doet de webserver van de ESP32 na, zodat je de bedieningspagina
zonder hardware kunt bekijken.

    python3 proefserver.py
    open http://localhost:8080 (of http://<ip-van-je-laptop>:8080 op de telefoon)

De kamertemperatuur loopt langzaam op als de verwarming aan staat en zakt als
hij uit staat. Er wordt niets naar een ketel gestuurd.
"""

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

POORT = 8080
JS = Path(__file__).with_name("www") / "thermostaat.js"

# Dezelfde opbouw als de pagina die ESPHome zelf maakt (build_index_html).
INDEX = (
    "<!DOCTYPE html><html><head><meta charset=UTF-8><link rel=icon href=data:>"
    "</head><body><script type=module src=/0.js></script><esp-app></esp-app>"
    "</body></html>"
)

slot = threading.Lock()
luisteraars: list[queue.Queue] = []

toestand = {
    "kamer": 19.2,
    "vocht": 52.0,
    "doel": 20.5,
    "modus": "HEAT",
    "schema": True,
    "comfort": 20.5,
    "eco": 16.5,
    "tijden": {
        "Ochtend": "06:30:00",
        "Avond": "22:30:00",
        "Weekendochtend": "08:00:00",
        "Weekendavond": "23:00:00",
    },
    "waterdruk": 1.6,
    "storing": False,
    "sensorstoring": False,
    "beweging": False,
}


def entiteiten() -> list[dict]:
    t = toestand
    vraagt = t["modus"] == "HEAT" and t["kamer"] < t["doel"] - 0.05
    aanvoer = 52.0 if vraagt else 31.0
    return [
        {
            "id": "climate/Thermostaat",
            "mode": t["modus"],
            "action": "HEATING" if vraagt else ("IDLE" if t["modus"] == "HEAT" else "OFF"),
            "state": "HEATING" if vraagt else "IDLE",
            "current_temperature": "NA" if t["sensorstoring"] else f"{t['kamer']:.1f}",
            "target_temperature": f"{t['doel']:.1f}",
            "min_temp": "10.0",
            "max_temp": "25.0",
            "step": 0.5,
        },
        {"id": "sensor/Kamertemperatuur", "value": None if t["sensorstoring"] else round(t["kamer"], 2)},
        {"id": "sensor/Luchtvochtigheid", "value": t["vocht"]},
        {"id": "sensor/Aanvoer", "value": aanvoer},
        {"id": "sensor/Retour", "value": aanvoer - 12},
        {"id": "sensor/Waterdruk", "value": t["waterdruk"]},
        {"id": "sensor/Modulatie", "value": 35.0 if vraagt else 0.0},
        {"id": "binary_sensor/Brander", "value": vraagt},
        {"id": "binary_sensor/Verwarming", "value": vraagt},
        {"id": "binary_sensor/Warmwater", "value": False},
        {"id": "binary_sensor/Storing", "value": t["storing"]},
        {"id": "binary_sensor/Sensorstoring", "value": t["sensorstoring"]},
        {"id": "binary_sensor/Beweging", "value": t["beweging"]},
        {"id": "number/Comfort", "value": f"{t['comfort']:.1f}"},
        {"id": "number/Eco", "value": f"{t['eco']:.1f}"},
        {"id": "switch/Schema", "value": t["schema"]},
    ] + [{"id": f"time/{naam}", "value": v} for naam, v in t["tijden"].items()]


def zend_alles() -> None:
    with slot:
        data = entiteiten()
        for q in luisteraars:
            for e in data:
                q.put(e)


def simulatie() -> None:
    while True:
        time.sleep(2)
        with slot:
            t = toestand
            if t["modus"] == "HEAT" and t["kamer"] < t["doel"]:
                t["kamer"] = min(t["doel"], t["kamer"] + 0.05)
            else:
                t["kamer"] = max(15.0, t["kamer"] - 0.02)
        zend_alles()


class Afhandeling(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # minder ruis in de terminal
        pass

    def stuur(self, code: int, inhoud: bytes = b"", soort: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", soort)
        self.send_header("Content-Length", str(len(inhoud)))
        self.end_headers()
        self.wfile.write(inhoud)

    def do_GET(self):
        pad = urlsplit(self.path).path
        if pad == "/":
            self.stuur(200, INDEX.encode(), "text/html; charset=utf-8")
        elif pad == "/0.js":
            self.stuur(200, JS.read_bytes(), "text/javascript; charset=utf-8")
        elif pad == "/events":
            self.events()
        else:
            self.stuur(404)

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q: queue.Queue = queue.Queue()
        with slot:
            luisteraars.append(q)
            for e in entiteiten():
                q.put(e)
        try:
            while True:
                try:
                    e = q.get(timeout=10)
                    regel = f"event: state\ndata: {json.dumps(e)}\n\n"
                except queue.Empty:
                    regel = "event: ping\ndata: {}\n\n"
                self.wfile.write(regel.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with slot:
                luisteraars.remove(q)

    def do_POST(self):
        deel = urlsplit(self.path)
        stukken = [unquote(s) for s in deel.path.strip("/").split("/")]
        args = {k: v[0] for k, v in parse_qs(deel.query).items()}
        t = toestand
        with slot:
            if stukken[:2] == ["climate", "Thermostaat"]:
                if "target_temperature" in args:
                    t["doel"] = float(args["target_temperature"])
                if "mode" in args:
                    t["modus"] = args["mode"]
            elif stukken[0] == "number" and stukken[1] in ("Comfort", "Eco"):
                t[stukken[1].lower()] = float(args["value"])
            elif stukken[0] == "time" and stukken[1] in t["tijden"]:
                t["tijden"][stukken[1]] = args["value"]
            elif stukken[:2] == ["switch", "Schema"]:
                t["schema"] = stukken[2] == "turn_on"
            # Om te testen: POST /proef/waterdruk?value=0.8, /proef/storing, /proef/sensorstoring, /proef/beweging
            elif stukken[0] == "proef":
                if stukken[1] == "waterdruk":
                    t["waterdruk"] = float(args["value"])
                elif stukken[1] in ("storing", "sensorstoring", "beweging"):
                    t[stukken[1]] = not t[stukken[1]]
            else:
                self.stuur(404)
                return
        self.stuur(200)
        zend_alles()


if __name__ == "__main__":
    threading.Thread(target=simulatie, daemon=True).start()
    print(f"Proefserver op http://localhost:{POORT}")
    ThreadingHTTPServer(("0.0.0.0", POORT), Afhandeling).serve_forever()
