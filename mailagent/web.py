"""Lokale goedkeuringsapp op http://127.0.0.1:8765. Alleen bereikbaar vanaf je eigen Mac.

De pagina (static/app.html) haalt haar gegevens op via /api/data en voert acties uit via
POST /api/<actie>. Elk verzoek naar /api/ moet de sessiesleutel in de header X-Token meesturen.

Bescherming tegen andere websites in je browser:
- de server antwoordt alleen als de Host-header 127.0.0.1 of localhost is (tegen DNS-rebinding,
  waarbij een vreemde site zich voordoet als deze pagina en zo de sessiesleutel zou kunnen lezen);
- een POST met een Origin-header van een andere site wordt geweigerd;
- de pagina mag niet in een frame van een andere site worden geladen (tegen ongemerkt aanklikken).
"""

from __future__ import annotations

import json
import secrets
import logging
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from datetime import date, datetime, timedelta

from .agenda import SOORTEN, afspraak_sleutel, mag_agenda_naar_claude, les_regel_bij, lees_werktijd, maak_agenda, soort_bepalen, vrije_momenten
from .config import anonimiseer, mag_naar_claude
from .models import Email
from .models import Sortering
from .opmaak import externe_afbeeldingen, html_uit_bron, veilig
from .pipeline import RondeBezig, terug_naar_te_doen, agenda_check, lijkt_afspraak, achtergrond_voor_item, afspraak_momenten, bevestig_voorlopig, geef_voorlopig_vrij, haal_uit_agenda, houd_vast, verzet_taak, afzender_adres, deadline_in_agenda, ochtend, voorstellen_voor, controleer_taken, controleer_uitgaand, is_belangrijk, plus_werkdagen, zet_taken_klaar, zet_taken_klaar_terug, map_voorstellen, markeer_gedaan, zet_gedaan_terug, zet_terug, plan_bestaande_taak, stijlvoorbeelden, keur_goed, maak_taak, markeer_mail, open_antwoord, overzicht, run, verplaats_items, wijs_af
from .melding import meld
from .store import Store

APP_HTML = Path(__file__).with_name("static") / "app.html"
# vaste bestanden naast de app: logo, Dock-icoon (Safari > File > Add to Dock) en manifest
BESTANDEN = {
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("dock-icoon.png", "image/png"),          # sommige Safari-versies nemen dit voor het Dock
    "/dock-icoon.png": ("dock-icoon.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/apple-touch-icon-precomposed.png": ("apple-touch-icon.png", "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}


def item_dict(rij, volledig: bool = False) -> dict:
    d = {k: rij[k] for k in ("id", "project", "actie", "prioriteit", "deadline", "samenvatting",
                             "status", "van", "onderwerp", "datum", "lijkt_beantwoord", "deadline_in_agenda")}
    if volledig:
        d.update({
            "reden": rij["reden"], "concept": rij["concept"] or "", "agenda": rij["agenda"],
            "vragen": rij["vragen"], "opmerking": rij["opmerking"], "context": json.loads(rij["context"] or "[]"),
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


def volgende_ronde(cfg: Config) -> str | None:
    """Eerstvolgende vaste mailronde op een werkdag, als 'ma 12:45' of '12:45' (vandaag)."""
    werk = lees_werktijd(cfg.agenda)
    nu = datetime.now()
    dagen_nl = ["ma", "di", "wo", "do", "vr", "za", "zo"]
    for plus in range(8):
        d = nu + timedelta(days=plus)
        if d.isoweekday() not in werk.dagen:
            continue
        for r in sorted(cfg.rondes):
            h, m = (int(x) for x in r.split(":"))
            t = d.replace(hour=h, minute=m, second=0, microsecond=0)
            if t > nu:
                return r if plus == 0 else f"{dagen_nl[t.weekday()]} {r}"
    return None


def te_doen_dict(cfg: Config, store: Store, r) -> dict:
    """Mail in Te doen, met of de afzender belangrijk is en, als hij al beantwoord lijkt, wat je stuurde."""
    it = store.item(r["id"])
    d = {**item_dict(it, True), "belangrijk": is_belangrijk(cfg, store, r["van"]), "jouw_antwoord": None}
    if it["lijkt_beantwoord"] and it["bewijs"]:
        v = store.verstuurd_bericht(it["bewijs"])
        if v is not None:
            d["jouw_antwoord"] = {"tekst": (v["tekst"] or "")[:1500], "onderwerp": v["onderwerp"], "datum": v["datum"]}
    return d


def wacht_dict(store: Store, w) -> dict:
    """Een regel 'wacht op antwoord', met mail die sindsdien van dezelfde mensen kwam (misschien in een andere draad)."""
    d = dict(w)
    ontvangers = {a.strip().lower() for a in (w["aan"] or "").split(",") if a.strip()}
    sindsdien = []
    try:
        na = datetime.fromisoformat(w["verstuurd"])
    except ValueError:
        na = None
    if na is not None and ontvangers:
        for m in store.inkomend_sinds((na - timedelta(days=1)).isoformat()):
            try:
                if datetime.fromisoformat(m["datum"]) <= na:
                    continue
            except ValueError:
                continue
            if afzender_adres(m["van"] or "") in ontvangers:
                it = store.db.execute("SELECT id, status FROM items WHERE sleutel=?", (m["sleutel"],)).fetchone()
                sindsdien.append({"onderwerp": m["onderwerp"], "datum": m["datum"], "van": m["van"],
                                  "item": it["id"] if it else None, "status": it["status"] if it else None})
    d["sindsdien"] = sorted(sindsdien, key=lambda x: x["datum"], reverse=True)[:3]
    return d


def verzamel(cfg: Config, store: Store, ophalen: dict) -> dict:
    werk = lees_werktijd(cfg.agenda)
    tellingen = {p: len(store.items(status="wacht", project=p)) for p in cfg.projecten}
    zelf = store.items(status="fout") + store.items(status="handmatig")
    return {
        "projecten": [{"naam": p, "wacht": tellingen[p]} for p in cfg.projecten],
        "wacht": [te_doen_dict(cfg, store, r) for r in store.items(status="wacht")],
        "zelf": [te_doen_dict(cfg, store, r) for r in zelf],
        "info": [item_dict(r) for r in store.items(status="ter_info")[:100]],
        "overgeslagen": [item_dict(r) for r in store.items(status="genegeerd")[:100]],
        "taken": [taak_dict(store, t) for t in store.open_taken()],
        "afgerond": [taak_dict(store, t, mail=False) for t in store.afgeronde_taken()],
        "regels": [dict(r) for r in store.afzender_regels()],
        **afzender_voorstellen(store),
        "ophalen": {"bezig": ophalen["bezig"], "laatste": ophalen["laatste"]},
        "werktijd": {"begin": werk.begin.strftime("%H:%M"), "eind": werk.eind.strftime("%H:%M"), "dagen": sorted(werk.dagen)},
        "agenda_aan": bool((cfg.agenda or {}).get("kalender")),
        "agenda_naam": (cfg.agenda or {}).get("kalender"),
        "vandaag_afgehandeld": store.vandaag_afgehandeld(),
        "volgende_ronde": volgende_ronde(cfg),
        "wacht_op": [wacht_dict(store, w) for w in store.wacht_op_lijst("wacht")],
        "ochtend": ochtend(cfg, store),
        "afgehandeld": [{**item_dict(r), "afgehandeld": r["afgehandeld"]} for r in store.afgehandeld_uit_te_doen()],
        "belangrijk": [{"adres": a, "vast": True} for a in cfg.belangrijk]
                      + [{"adres": r["adres"], "naam": r["naam"], "vast": False} for r in store.belangrijke()],
    }


def maak_handler(cfg: Config, store: Store, bronnen: dict, llm_factory):
    token = secrets.token_urlsafe(24)
    db_pad = store.pad
    ophalen = {"bezig": False, "laatste": ""}
    mail_slot = threading.Lock()  # Apple Mail krijgt één opdracht tegelijk
    agenda_cache: dict = {}
    mappen_cache: dict = {}   # bron -> lijst mappen; eenmaal per sessie opgehaald
    html_cache: dict = {}     # item-id -> veilige HTML

    def agenda():
        """Agenda pas koppelen als hij nodig is; een fout wordt onthouden tot de volgende poging."""
        if "agenda" not in agenda_cache or agenda_cache["agenda"] is None:
            agenda_cache["agenda"], agenda_cache["fout"] = maak_agenda(cfg)
        return agenda_cache["agenda"], agenda_cache["fout"]

    def taakvoorstel(store: Store, item_id: int) -> dict:
        it = store.item(item_id)
        if it is None:
            raise ValueError("unknown email")
        mail = Email(bron=it["bron"], bron_id=it["bron_id"], van=it["van"] or "", aan=it["aan"] or "",
                     onderwerp=it["onderwerp"] or "", datum=datetime.fromisoformat(it["datum"]), tekst=it["tekst"] or "")
        if mag_naar_claude(cfg, mail):
            extra, _ = achtergrond_voor_item(cfg, store, it)
            plan = llm_factory().plan_taak(mail, anonimiseer(cfg, mail.tekst), it["project"], achtergrond=extra or None)
            voorstel = plan.model_dump()
        else:
            voorstel = {"titel": f"Deal with: {mail.onderwerp}", "stappen": [], "minuten": 30,
                        "deadline": it["deadline"], "toelichting": "This sender is not sent to Claude, so the default is 30 minutes."}
        voorstel["deadline"] = voorstel["deadline"] or it["deadline"]
        factor, n = store.tijdfactor(it["project"])
        if n >= 3 and abs(factor - 1) >= 0.1:
            voorstel["minuten"] = max(15, min(480, round(voorstel["minuten"] * factor / 15) * 15))
            voorstel["toelichting"] += (f" Adjusted by ×{factor:.1f}, because your last {n} tasks in {it['project']} "
                                        f"took {'longer' if factor > 1 else 'less time'} than estimated.")
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
                voorstel["agenda_fout"] = f"Could not read the calendar: {ex}"
        return voorstel

    def ophalen_op_achtergrond():
        try:
            rest: dict = {}
            res = run(cfg, Store(db_pad), bronnen, llm_factory(), voortgang=rest, agenda=lambda: agenda()[0])
            ophalen["laatste"] = ", ".join(
                f"{k}: failed" if v < 0 else f"{k}: {v} new" + (f", {rest[k]} still to go" if rest.get(k) else "")
                for k, v in res.items())
        except RondeBezig as ex:
            ophalen["laatste"] = str(ex)
        except Exception as ex:
            ophalen["laatste"] = f"getting mail failed: {ex}"
        finally:
            ophalen["bezig"] = False

    indelen = {"bezig": False}
    indelen_slot = threading.Lock()

    def deel_in_op_achtergrond(afspraken: list[dict]) -> bool:
        """Afspraken die de app nog niet kent laten indelen door Claude, op de achtergrond, één keer per reeks.
        Niets van organisatoren of titels op de lijst 'nooit naar Claude'; notities kort en zonder links."""
        import re as _re
        if not indelen_slot.acquire(blocking=False):
            return True   # loopt al
        indelen["bezig"] = True

        def werk():
            st = Store(db_pad)
            try:
                gezien, stukken = set(), []
                for a in afspraken:
                    sleutel = afspraak_sleutel(a)
                    org = a.get("organisator") or {}
                    proef = Email(bron="", bron_id="", van=org.get("adres") or org.get("naam") or "", aan="",
                                  onderwerp=a.get("titel") or "", datum=datetime.now().astimezone(), tekst="")
                    if sleutel in gezien or not mag_naar_claude(cfg, proef) or not mag_agenda_naar_claude(cfg.agenda, a):
                        continue   # ook geen afspraken uit je gezins- of privé-agenda
                    gezien.add(sleutel)
                    dl = [d for d in a.get("deelnemers") or [] if not d.get("ik")]
                    notities = _re.sub(r"https?://\S+", "", a.get("notities") or "")[:300].strip()
                    stukken.append((sleutel, a.get("titel"), {
                        "titel": anonimiseer(cfg, a.get("titel") or ""), "locatie": a.get("locatie"),
                        "organisator": org.get("naam") or org.get("adres"),
                        "deelnemers": f"{len(dl)} anderen" + (f", bijvoorbeeld {', '.join(d.get('naam') or d.get('adres', '') for d in dl[:6])}" if dl else ""),
                        "herhaalt": "ja" if a.get("herhaalt") else "nee", "agenda": a.get("kalender"),
                        "duur": f"{int((a['eind'] - a['start']).total_seconds() // 60)} minuten",
                        "notities": anonimiseer(cfg, notities)}))
                llm = llm_factory()
                if not hasattr(llm, "deel_afspraken_in"):
                    return
                for begin in range(0, len(stukken), 20):
                    groep = stukken[begin:begin + 20]
                    uit = llm.deel_afspraken_in([{"nr": i, **velden} for i, (_, _, velden) in enumerate(groep)])
                    for r in uit:
                        if 0 <= int(r.get("nr", -1)) < len(groep) and r.get("soort") in SOORTEN:
                            sleutel, titel, _ = groep[int(r["nr"])]
                            st.zet_afspraak_soort(sleutel, r["soort"], (r.get("reden") or "")[:120], "claude", titel)
            except Exception as e:
                logging.getLogger("mailagent").warning("afspraken indelen: %s", e)
            finally:
                indelen["bezig"] = False
                indelen_slot.release()
        threading.Thread(target=werk, daemon=True).start()
        return True

    def bewaak_belangrijk() -> int:
        """Eén keer kijken: nieuwe mail van een belangrijke afzender? Dan een melding op je Mac, en meteen
        een ronde zodat hij met concept in Te doen staat. Zonder Claude; alleen afzender en onderwerp."""
        st = Store(db_pad)
        gevonden = 0
        for naam, bron in bronnen.items():
            if not hasattr(bron, "recente"):
                continue
            try:
                with mail_slot:
                    recent = bron.recente(cfg.meldingen_minuten * 60 + 300)
            except Exception as e:
                logging.getLogger("mailagent").warning("bewaking %s: %s", naam, e)
                continue
            for bid, van, onderwerp in recent:
                sleutel = f"{naam}:{bid}"
                if st.is_gemeld(sleutel) or not is_belangrijk(cfg, st, van):
                    continue
                st.zet_gemeld(sleutel)
                meld(f"Email from {van.split('<')[0].strip() or van}", onderwerp or "(no subject)")
                gevonden += 1
        if gevonden and not ophalen["bezig"]:
            ophalen["bezig"], ophalen["laatste"] = True, ""
            threading.Thread(target=ophalen_op_achtergrond, daemon=True).start()
        return gevonden

    def start_bewaking() -> None:
        def lus():
            while True:
                if 7 <= datetime.now().hour < 22:
                    try:
                        bewaak_belangrijk()
                    except Exception as e:
                        logging.getLogger("mailagent").warning("bewaking: %s", e)
                time.sleep(cfg.meldingen_minuten * 60)
        threading.Thread(target=lus, daemon=True).start()

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
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            try:
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser was al weg; de actie zelf is wel uitgevoerd

        def _json(self, obj: dict, status: int = 200):
            self._stuur(json.dumps(obj, ensure_ascii=False), status, "application/json; charset=utf-8")

        def _eigen_adressen(self) -> set[str]:
            poort = self.server.server_address[1]
            namen = {"127.0.0.1", "localhost", "[::1]", cfg.web_host}
            return {f"{n}:{poort}" for n in namen}

        def _host_ok(self) -> bool:
            """Alleen verzoeken die echt voor 127.0.0.1/localhost bedoeld zijn (DNS-rebinding)."""
            return (self.headers.get("Host") or "").strip().lower() in self._eigen_adressen()

        def _origin_ok(self) -> bool:
            """Een browser stuurt bij POST een Origin mee; die moet deze app zelf zijn."""
            origin = (self.headers.get("Origin") or "").strip().lower()
            return not origin or origin in {f"http://{a}" for a in self._eigen_adressen()}

        def _token_ok(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Token", ""), token)

        def _weiger(self, status: int = 403):
            return self._json({"ok": False, "melding": "Invalid session. Reload the page."}, status)

        def do_GET(self):
            if not self._host_ok():
                return self._stuur("Unknown address", 421, "text/plain; charset=utf-8")
            pad = urlparse(self.path).path
            if pad == "/":
                html = APP_HTML.read_text(encoding="utf-8").replace("__TOKEN__", token)
                return self._stuur(html)
            if pad in BESTANDEN:                      # logo en iconen: geen mail, dus geen sessiesleutel nodig
                naam, soort = BESTANDEN[pad]
                return self._stuur((APP_HTML.parent / naam).read_bytes(), soort=soort)
            if pad.startswith("/api/") and not self._token_ok():
                return self._weiger()
            store = Store(db_pad)  # eigen verbinding per verzoek: verzoeken lopen naast elkaar
            if pad == "/api/data":
                return self._json(verzamel(cfg, store, ophalen))
            if pad == "/api/overzicht.md":
                return self._stuur(overzicht(cfg, store), soort="text/markdown; charset=utf-8")
            self._stuur("Not found", 404, "text/plain; charset=utf-8")

        def do_POST(self):
            if not self._host_ok():
                return self._stuur("Unknown address", 421, "text/plain; charset=utf-8")
            if not self._origin_ok() or not self._token_ok():
                return self._weiger()
            lengte = int(self.headers.get("Content-Length", 0) or 0)
            try:
                v = json.loads(self.rfile.read(lengte) or b"{}")
            except json.JSONDecodeError:
                return self._json({"ok": False, "melding": "Invalid request"}, 400)
            actie = urlparse(self.path).path.removeprefix("/api/")
            store = Store(db_pad)
            try:
                if actie == "goedkeuren":
                    with mail_slot:
                        melding = keur_goed(store, bronnen, int(v["id"]), v.get("concept"), v.get("taken"),
                                            verzenden=bool(v.get("verzenden")))
                    if v.get("verzenden") and store.item(int(v["id"]))["status"] != "verzonden":
                        return self._json({"ok": False, "melding": melding})
                elif actie == "gedaan":
                    melding = markeer_gedaan(store, int(v["id"]))
                elif actie == "niet-gedaan":
                    melding = zet_gedaan_terug(store, int(v["id"]))
                elif actie == "afwijzen":
                    wijs_af(store, int(v["id"]))
                    melding = "Afgewezen"
                elif actie in ("taak-klaar", "taak-heropen"):
                    klaar = actie == "taak-klaar"
                    (store.taak_klaar if klaar else store.taak_heropen)(int(v["id"]))
                    melding = "Task ticked off" if klaar else "Task restored"
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
                        return self._json({"ok": False, "melding": "This email can't be opened in Mail"})
                    subprocess.run(["open", f"message://%3c{mid}%3e"], check=False, timeout=20)
                    melding = ""
                elif actie == "antwoord-concept":
                    it = store.item(int(v["id"]))
                    mail = Email(bron=it["bron"], bron_id=it["bron_id"], van=it["van"] or "", aan=it["aan"] or "",
                                 onderwerp=it["onderwerp"] or "", datum=datetime.fromisoformat(it["datum"]), tekst=it["tekst"] or "")
                    if not mag_naar_claude(cfg, mail):
                        return self._json({"ok": False, "melding": "This sender is not sent to Claude, so write the reply yourself"})
                    s = Sortering(project=it["project"], actie="antwoorden", prioriteit=it["prioriteit"],
                                  deadline=it["deadline"], samenvatting=it["samenvatting"] or it["onderwerp"], reden="")
                    extra, context = achtergrond_voor_item(cfg, store, it)
                    llm = llm_factory()
                    tekst_a = anonimiseer(cfg, mail.tekst)
                    check = None
                    if lijkt_afspraak(it["actie"], mail.tekst):
                        ag, _ = agenda()
                        if ag is not None:
                            try:
                                verzoek = json.loads(it["afspraak"]) if it["afspraak"] else llm.afspraak_uit_mail(mail, tekst_a, extra or None)
                                store.zet_afspraak_verzoek(it["id"], verzoek)
                                check = agenda_check(cfg, ag, verzoek)
                                context = context + [{"soort": "agenda", **check["telling"]}]
                            except Exception as ex:
                                logging.getLogger("mailagent").warning("agenda nakijken: %s", ex)
                    u = llm.werk_uit(mail, tekst_a, s, stijlvoorbeelden(cfg, store, mail, it["project"]),
                                     achtergrond=extra or None, agenda_check=check["blok"] if check else None)
                    if context:
                        store.zet_context(it["id"], context)
                    if not (u.concept_antwoord or "").strip():
                        return self._json({"ok": False, "melding": "Claude found no reply was needed and wrote no draft"})
                    store.zet_concept(it["id"], u.concept_antwoord.strip(), vragen=u.vragen_aan_jou or "")
                    return self._json({"ok": True, "concept": u.concept_antwoord.strip(), "vragen": u.vragen_aan_jou})
                elif actie == "taken-klaarzetten":
                    item_id = int(v["id"])
                    melding, ids = zet_taken_klaar(store, bronnen, item_id, v.get("concept"), v.get("taken") or [])
                    if cfg.vlag_aan:
                        def vlag(item_id=item_id):
                            with mail_slot:
                                markeer_mail(Store(db_pad), bronnen, item_id, True, cfg.vlag_kleur)
                        threading.Thread(target=vlag, daemon=True).start()   # niet wachten op Apple Mail
                    return self._json({"ok": True, "melding": melding, "taken": ids})
                elif actie == "taken-controleren":
                    tel = controleer_uitgaand(cfg, store, bronnen, llm_factory(), slot=mail_slot)
                    delen = []
                    if tel["taken"]:
                        delen.append("1 task looks done already" if tel["taken"] == 1 else f"{tel['taken']} tasks look done already")
                    if tel["beantwoord"]:
                        delen.append("1 email in To do looks answered already" if tel["beantwoord"] == 1
                                     else f"{tel['beantwoord']} emails in To do look answered already")
                    if tel["wacht_opgelost"]:
                        delen.append(f"{tel['wacht_opgelost']} awaited " + ("reply came in" if tel["wacht_opgelost"] == 1 else "replies came in"))
                    melding = "; ".join(delen) or "Nothing new found"
                elif actie == "niet-beantwoord":
                    store.wis_lijkt_beantwoord(int(v["id"]))
                    melding = "Stays in To do"
                elif actie == "belangrijk":
                    adres = (v.get("adres") or "").strip().lower()
                    if not adres:
                        return self._json({"ok": False, "melding": "No address"})
                    if v.get("aan", True):
                        store.zet_belangrijk(adres, v.get("naam"))
                        melding = f"{v.get('naam') or adres} is now an important sender"
                    else:
                        if adres in cfg.belangrijk:
                            return self._json({"ok": False, "melding": "This address is listed under belangrijk: in config.yaml; remove it there"})
                        store.verwijder_belangrijk(adres)
                        melding = f"{v.get('naam') or adres} is no longer an important sender"
                elif actie in ("wacht-klaar", "wacht-later", "wacht-terug"):
                    w = store.wacht_op(int(v["id"]))
                    if w is None:
                        return self._json({"ok": False, "melding": "Not found"})
                    if actie == "wacht-klaar":
                        store.zet_wacht_op(w["id"], status="klaar")
                        melding = "Off the list"
                    elif actie == "wacht-terug":
                        store.zet_wacht_op(w["id"], status="wacht")
                        melding = "Back on the list"
                    else:
                        dagen = int(v.get("werkdagen") or 3)
                        terug = plus_werkdagen(date.today(), dagen, lees_werktijd(cfg.agenda).dagen)
                        store.zet_wacht_op(w["id"], herinner_op=terug.isoformat())
                        melding = f"Comes back on {terug.day} {terug:%b}"
                elif actie == "wacht-herinnering":
                    w = store.wacht_op(int(v["id"]))
                    if w is None:
                        return self._json({"ok": False, "melding": "Not found"})
                    if not (w["tekst"] or "").strip():
                        return self._json({"ok": False, "melding": "The text of your email has not been saved yet; that happens in the next round. Until then, open it in Mail"})
                    ontv = Email(bron="", bron_id="", van=w["aan"] or "", aan="", onderwerp=w["onderwerp"] or "",
                                 datum=datetime.now().astimezone(), tekst="")
                    if not mag_naar_claude(cfg, ontv):
                        return self._json({"ok": False, "melding": "This recipient is not sent to Claude, so write the reminder yourself"})
                    werk = lees_werktijd(cfg.agenda)
                    dagen, d = 0, datetime.fromisoformat(w["verstuurd"]).date()
                    while d < date.today():
                        d += timedelta(days=1)
                        dagen += d.isoweekday() in werk.dagen
                    concept = llm_factory().herinnering(w["aan"] or "", w["onderwerp"] or "",
                                                        anonimiseer(cfg, w["tekst"]), dagen)
                    store.zet_wacht_concept(w["id"], concept)
                    return self._json({"ok": True, "concept": concept})
                elif actie == "wacht-concept":
                    store.zet_wacht_concept(int(v["id"]), v.get("concept") or "")
                    melding = ""
                elif actie == "wacht-openen":
                    w = store.wacht_op(int(v["id"]))
                    mid = (w["message_id"] or "").strip().strip("<>") if w else ""
                    if not mid:
                        return self._json({"ok": False, "melding": "This email can't be opened in Mail"})
                    subprocess.run(["open", f"message://%3c{mid}%3e"], check=False, timeout=20)
                    melding = ""
                elif actie == "taak-niet-klaar":
                    store.wis_lijkt_klaar(int(v["id"]))
                    melding = "Stays open"
                elif actie == "taken-terug":
                    item_id = int(v["id"])
                    melding = zet_taken_klaar_terug(store, bronnen, item_id, [int(x) for x in v.get("taken", [])])
                    if cfg.vlag_aan and not any(t["item_id"] == item_id for t in store.open_taken()):
                        def vlag_weg(item_id=item_id):
                            with mail_slot:
                                markeer_mail(Store(db_pad), bronnen, item_id, False, cfg.vlag_kleur)
                        threading.Thread(target=vlag_weg, daemon=True).start()
                elif actie == "antwoord-openen":
                    with mail_slot:
                        melding = open_antwoord(store, bronnen, int(v["id"]), v.get("tekst", ""))
                elif actie == "verplaats":
                    ids = [int(x) for x in v.get("ids", [])]
                    if not ids:
                        return self._json({"ok": False, "melding": "Nothing selected"})
                    if v.get("doel") == "overslaan":
                        adressen = set()
                        for i in ids:
                            it = store.item(i)
                            if it:
                                adres = afzender_adres(it["van"])
                                adressen.add(adres)
                                store.zet_afzender_regel(adres, "negeren", it["project"])
                        melding = (f"{len(adressen)} senders will be skipped from now on" if len(adressen) != 1 else "1 sender will be skipped from now on")
                    else:
                        with mail_slot:
                            melding = verplaats_items(store, bronnen, ids, v.get("doel", ""))
                        if "failed" in melding:
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
                    lessen = (cfg.agenda or {}).get("lessen") or {}
                    regels, geleerd = store.les_regels(), store.afspraak_soorten()
                    uit, onbekend = [], []
                    for a in lijst:
                        sleutel = afspraak_sleutel(a)
                        g = geleerd.get(sleutel)
                        soort, waarom = soort_bepalen(a, lessen, regels, g)
                        if g is None and not a.get("heledag") and not a.get("vrij") and not (a.get("id") and store.voorlopig(a["id"])):
                            onbekend.append(a)
                        uit.append({**a, "start": a["start"].isoformat(timespec="minutes"),
                                    "eind": a["eind"].isoformat(timespec="minutes"), "sleutel": sleutel,
                                    "voorlopig": bool(a.get("id")) and store.voorlopig(a["id"]) is not None,
                                    "soort": soort, "soort_waarom": waarom, "soort_door": (g or {}).get("door", ""),
                                    "les_regel": les_regel_bij(a, regels)})
                    bezig = deel_in_op_achtergrond(onbekend) if onbekend else indelen["bezig"]
                    return self._json({"ok": True, "afspraken": uit, "bezig": bool(bezig)})
                elif actie == "zoek":
                    res = store.zoek(str(v.get("q") or "")[:200])
                    return self._json({"ok": True,
                                       "mail": [{**item_dict(r), "afgehandeld": r["afgehandeld"]} for r in res["mail"]],
                                       "taken": [{"id": t["id"], "omschrijving": t["omschrijving"], "gepland": t["gepland"],
                                                  "klaar": t["klaar"], "project": t["project"], "item_id": t["item_id"]} for t in res["taken"]],
                                       "wacht": [{"id": w["id"], "onderwerp": w["onderwerp"], "aan": w["aan"], "verstuurd": w["verstuurd"]}
                                                 for w in res["wacht"]]})
                elif actie == "item-detail":
                    it = store.item(int(v["id"]))
                    if it is None:
                        return self._json({"ok": False, "melding": "Email not found"})
                    taken = [dict(t) for t in store.db.execute("SELECT * FROM taken WHERE item_id=? ORDER BY id", (it["id"],))]
                    return self._json({"ok": True, "mail": {**item_dict(it, True), "eigen_antwoord": it["eigen_antwoord"],
                                                            "afgehandeld": it["afgehandeld"], "taken_db": taken}})
                elif actie == "terug-naar-te-doen":
                    with mail_slot:
                        melding = terug_naar_te_doen(store, bronnen, int(v["id"]))
                elif actie == "afspraak-voorstel":
                    it = store.item(int(v["id"]))
                    if it is None:
                        return self._json({"ok": False, "melding": "Email not found"})
                    mail = Email(bron=it["bron"], bron_id=it["bron_id"], van=it["van"] or "", aan=it["aan"] or "",
                                 onderwerp=it["onderwerp"] or "", datum=datetime.fromisoformat(it["datum"]), tekst=it["tekst"] or "")
                    verzoek = json.loads(it["afspraak"]) if it["afspraak"] and not v.get("opnieuw") else None
                    if verzoek is None:
                        if not mag_naar_claude(cfg, mail):
                            return self._json({"ok": False, "melding": "This sender is not sent to Claude, so schedule the meeting yourself"})
                        extra, _ = achtergrond_voor_item(cfg, store, it)
                        verzoek = llm_factory().afspraak_uit_mail(mail, anonimiseer(cfg, mail.tekst), extra or None)
                        store.zet_afspraak_verzoek(it["id"], verzoek)
                    morgen = date.today() + timedelta(days=1)
                    def dag(x, standaard):
                        try:
                            return date.fromisoformat(str(x)[:10]) if x else standaard
                        except ValueError:
                            return standaard
                    vanaf = dag(v.get("vanaf"), max(morgen, dag(verzoek.get("vroegst"), morgen)))
                    uiterlijk = dag(v.get("uiterlijk"), dag(verzoek.get("uiterlijk"), vanaf + timedelta(days=14)))
                    if uiterlijk < vanaf:
                        uiterlijk = vanaf + timedelta(days=7)
                    duur = max(15, min(480, int(v.get("duur") or verzoek.get("duur_minuten") or 30)))
                    dagdeel = v.get("dagdeel") if v.get("dagdeel") in ("ochtend", "middag") else ""
                    ag, fout = agenda()
                    momenten = afspraak_momenten(cfg, ag, verzoek, vanaf, uiterlijk, dagdeel, duur) if ag else {"voorgesteld": [], "vrij": []}
                    return self._json({"ok": True, "verzoek": verzoek, **momenten, "agenda_fout": fout if ag is None else None,
                                       "vw": {"vanaf": vanaf.isoformat(), "uiterlijk": uiterlijk.isoformat(), "dagdeel": dagdeel, "duur": duur}})
                elif actie == "afspraak-concept":
                    it = store.item(int(v["id"]))
                    mail = Email(bron=it["bron"], bron_id=it["bron_id"], van=it["van"] or "", aan=it["aan"] or "",
                                 onderwerp=it["onderwerp"] or "", datum=datetime.fromisoformat(it["datum"]), tekst=it["tekst"] or "")
                    if not mag_naar_claude(cfg, mail):
                        return self._json({"ok": False, "melding": "This sender is not sent to Claude, so add the times to your reply yourself"})
                    concept = llm_factory().concept_met_momenten(
                        mail, anonimiseer(cfg, mail.tekst), v.get("concept") or it["concept"] or "",
                        [str(m) for m in v.get("momenten", [])], [str(m) for m in v.get("niet", [])], v.get("locatie"))
                    store.zet_concept(it["id"], concept)
                    return self._json({"ok": True, "concept": concept})
                elif actie == "afspraak-vasthouden":
                    ag, fout = agenda()
                    with mail_slot:
                        melding = houd_vast(store, ag, int(v["id"]), [str(m) for m in v.get("momenten", [])],
                                            int(v.get("duur") or 30), v.get("onderwerp") or "Afspraak", v.get("met") or "")
                elif actie in ("voorlopig-bevestig", "voorlopig-vrij"):
                    ag, fout = agenda()
                    if ag is None:
                        return self._json({"ok": False, "melding": fout or "no calendar connected"})
                    with mail_slot:
                        melding = (bevestig_voorlopig if actie == "voorlopig-bevestig" else geef_voorlopig_vrij)(store, ag, v["afspraak"])
                    melding = melding[:1].upper() + melding[1:]
                elif actie == "afspraak-soort":
                    sleutel = v.get("sleutel") or ""
                    if not sleutel:
                        return self._json({"ok": False, "melding": "Unknown event"})
                    if v.get("soort") in SOORTEN:
                        store.zet_afspraak_soort(sleutel, v["soort"], None, "jij", v.get("titel"))
                        melding = {"les": "Class", "afdeling": "Department meeting", "overleg": "Meeting with others",
                                   "eigen": "Personal event"}[v["soort"]] + (", for future occurrences too" if v.get("herhaalt") else "")
                    else:
                        store.wis_afspraak_soort(sleutel)
                        melding = "The app will classify this event itself again"
                elif actie == "les-regel":
                    soort = v.get("soort")
                    waarde = (v.get("waarde") or "").strip()
                    if soort not in ("titel", "organisator", "kalender") or not waarde:
                        return self._json({"ok": False, "melding": "Unknown rule"})
                    store.zet_les_regel(soort, waarde, bool(v.get("les", True)))
                    wat = {"titel": f"titled \u2018{waarde}\u2019", "organisator": f"from {v.get('label') or waarde}",
                           "kalender": f"in the calendar \u2018{waarde}\u2019"}[soort]
                    melding = (f"Events {wat} now count as classes" if v.get("les", True)
                               else f"Events {wat} no longer count as classes")
                elif actie == "les-regel-weg":
                    store.verwijder_les_regel(int(v["id"]))
                    melding = "Rule removed"
                elif actie == "taak-maak":
                    start = datetime.fromisoformat(v["start"]).astimezone() if v.get("start") else None
                    stappen = [s.strip() for s in v.get("stappen", []) if s.strip()]
                    ag = agenda()[0] if start else None
                    with mail_slot:
                        melding = maak_taak(store, bronnen, ag, int(v["id"]), v["titel"].strip() or "Taak",
                                            stappen, int(v.get("minuten") or 30), v.get("deadline") or None,
                                            start, bool(v.get("archiveren")),
                                            vlag=cfg.vlag_kleur if cfg.vlag_aan else None)
                elif actie == "taak-plannen":
                    ag, fout = agenda()
                    if ag is None:
                        return self._json({"ok": False, "melding": fout or "no calendar connected"})
                    start = datetime.fromisoformat(v["start"]).astimezone()
                    with mail_slot:
                        melding = plan_bestaande_taak(store, ag, int(v["id"]), start, int(v.get("minuten") or 30))
                elif actie == "project":
                    if v.get("project") not in cfg.projecten:
                        return self._json({"ok": False, "melding": "Unknown project"})
                    store.zet_project(int(v["id"]), v["project"])
                    melding = f"Moved to {v['project']}"
                elif actie == "mail":
                    it = store.item(int(v["id"]))
                    if it is None:
                        return self._json({"ok": False, "melding": "Email not found"})
                    return self._json({"ok": True, "mail": item_dict(it, True)})
                elif actie == "mail-html":
                    i = int(v["id"])
                    it = store.item(i)
                    if it is None:
                        return self._json({"ok": False, "melding": "Email not found"})
                    adres = afzender_adres(it["van"] or "")
                    altijd = bool(adres) and store.afbeeldingen_ok(adres)
                    extern = bool(v.get("afbeeldingen")) or altijd
                    if i not in html_cache:
                        bron = bronnen.get(it["bron"])
                        if bron is None or not hasattr(bron, "bron"):
                            return self._json({"ok": False, "melding": "No formatted version available"})
                        with mail_slot:
                            ruw = bron.bron(it["bron_id"], it["thread_id"])
                        h = html_uit_bron(ruw)
                        if h is None:
                            return self._json({"ok": False, "melding": "No formatted version found"})
                        html_cache[i] = h          # ruwe HTML (met ingesloten afbeeldingen); veilig maken per verzoek
                        while len(html_cache) > 60:   # niet eindeloos groeien
                            html_cache.pop(next(iter(html_cache)))
                    h = html_cache[i]
                    return self._json({"ok": True, "html": veilig(h, extern=extern), "extern": extern, "altijd": altijd,
                                       "geblokkeerd": 0 if extern else externe_afbeeldingen(h), "adres": adres})
                elif actie == "afbeeldingen-altijd":
                    adres = (v.get("adres") or "").strip().lower()
                    if not adres:
                        return self._json({"ok": False, "melding": "No sender"})
                    store.zet_afbeeldingen_ok(adres, bool(v.get("aan", True)))
                    melding = (f"Images from {adres} will now load straight away" if v.get("aan", True)
                               else f"Images from {adres} will be blocked again")
                elif actie == "mappen":
                    it = store.item(int(v["id"]))
                    if it is None:
                        return self._json({"ok": False, "melding": "Email not found"})
                    bron = bronnen.get(it["bron"])
                    if bron is None or not hasattr(bron, "mappenlijst"):
                        return self._json({"ok": False, "melding": "Filing in folders isn't available for this account"})
                    if v.get("vernieuw") or it["bron"] not in mappen_cache:
                        with mail_slot:
                            mappen_cache[it["bron"]] = bron.mappenlijst()
                    mappen = mappen_cache[it["bron"]]
                    return self._json({"ok": True, "mappen": mappen, "voorstellen": map_voorstellen(store, it, mappen),
                                       "inbox": getattr(bron, "mailbox", ""),
                                       "archief": getattr(bron, "mappen", {}).get("archief", ""),
                                       "nieuw_kan": hasattr(bron, "maak_map")})
                elif actie == "map-maken":
                    it = store.item(int(v["id"]))
                    bron = bronnen.get(it["bron"]) if it else None
                    naam = (v.get("naam") or "").strip().strip("/")
                    if bron is None or not hasattr(bron, "maak_map"):
                        return self._json({"ok": False, "melding": "Creating a folder isn't available for this account"})
                    if not naam or len(naam) > 120:
                        return self._json({"ok": False, "melding": "Give the folder a name"})
                    pad = bron.nieuw_pad(naam)
                    bestaand = mappen_cache.get(it["bron"]) or []
                    if pad.lower() not in {m.lower() for m in bestaand}:
                        with mail_slot:
                            bron.maak_map(pad)
                        mappen_cache[it["bron"]] = bestaand + [pad]
                    return self._json({"ok": True, "pad": pad, "melding": f"Folder '{naam}' created"})
                elif actie == "terugzetten":
                    with mail_slot:
                        melding = zet_terug(store, bronnen, [int(x) for x in v.get("ids", [])])
                    if "failed" in melding:
                        return self._json({"ok": False, "melding": melding})
                elif actie == "taak-oordeel":
                    store.taak_oordeel(int(v["id"]), int(v["oordeel"]))
                    melding = "Thanks, future estimates will take this into account"
                elif actie == "taak-later":
                    store.taak_later(int(v["id"]), bool(v.get("later", True)))
                    melding = "Moved to Later" if v.get("later", True) else "Back in the plan"
                elif actie == "afsluiten":
                    ag, fout = agenda()
                    open_ = [t for t in store.open_taken() if not t["gepland"] and not t["later"]]
                    voorstellen = {}
                    if ag is not None:
                        morgen = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).astimezone()
                        voorstellen = voorstellen_voor(ag, cfg, open_, morgen)
                    return self._json({"ok": True, "voorstellen": voorstellen, "agenda_fout": fout})
                elif actie == "afspraak-verplaats":
                    ag, fout = agenda()
                    if ag is None:
                        return self._json({"ok": False, "melding": fout or "no calendar connected"})
                    start = datetime.fromisoformat(v["start"]).astimezone()
                    minuten = max(5, min(24 * 60, int(v.get("minuten") or 30)))
                    with mail_slot:
                        if v.get("taak"):
                            melding = verzet_taak(store, ag, int(v["taak"]), start, minuten)
                        else:
                            if not hasattr(ag, "verplaats_afspraak"):
                                return self._json({"ok": False, "melding": "Events can't be moved in this calendar"})
                            oud = datetime.fromisoformat(v["oud_start"]).astimezone() if v.get("oud_start") else None
                            melding = ag.verplaats_afspraak(v["id"], oud, start, start + timedelta(minutes=minuten))
                    melding = melding[:1].upper() + melding[1:]
                elif actie == "afspraak-weg":
                    ag, fout = agenda()
                    if ag is None:
                        return self._json({"ok": False, "melding": fout or "no calendar connected"})
                    melding = haal_uit_agenda(store, ag, int(v["id"]))
                elif actie == "agenda-openen":
                    # de app Agenda op de dag (en het uur) van deze afspraak
                    seconden = int((datetime.fromisoformat(v["start"]) - datetime.now().astimezone()).total_seconds())
                    script = ('on run argv\ntell application "Calendar"\nactivate\nswitch view to day view\n'
                              'view calendar at ((current date) + ((item 1 of argv) as integer))\nend tell\nend run')
                    subprocess.run(["osascript", "-e", script, str(seconden)], check=False, timeout=20, capture_output=True)
                    melding = ""
                elif actie == "deadline-voorstellen":
                    ag, fout = agenda()
                    if ag is None:
                        return self._json({"ok": False, "melding": fout or "no calendar connected"})
                    ids = set(ochtend(cfg, store)["zonder_moment"])
                    taken = [t for t in store.open_taken() if t["id"] in ids]
                    return self._json({"ok": True, "voorstellen": voorstellen_voor(ag, cfg, taken, datetime.now().astimezone())})
                elif actie == "deadline-agenda":
                    ag, fout = agenda()
                    if ag is None:
                        return self._json({"ok": False, "melding": fout or "no calendar connected"})
                    with mail_slot:
                        melding = deadline_in_agenda(store, ag, "taak" if v.get("soort") == "taak" else "mail", int(v["id"]))
                elif actie == "regels-toevoegen":
                    adressen = [a.strip().lower() for a in v.get("adressen", []) if a.strip()]
                    for a in adressen:
                        store.zet_afzender_regel(a, "negeren", None)
                    melding = (f"{len(adressen)} senders will be skipped from now on" if len(adressen) != 1 else "1 sender will be skipped from now on")
                elif actie == "regel-weg":
                    store.verwijder_afzender_regel(v["adres"])
                    melding = f"{v['adres']} is no longer skipped"
                elif actie == "ophalen":
                    if ophalen["bezig"]:
                        melding = "Already getting mail"
                    else:
                        ophalen["bezig"], ophalen["laatste"] = True, ""
                        threading.Thread(target=ophalen_op_achtergrond, daemon=True).start()
                        melding = "Getting new mail"
                else:
                    return self._json({"ok": False, "melding": "Unknown action"}, 404)
            except Exception as ex:
                return self._json({"ok": False, "melding": f"Fout: {ex}"})
            self._json({"ok": True, "melding": melding})

    Handler.bewaak_belangrijk = staticmethod(bewaak_belangrijk)
    Handler.deel_in = staticmethod(deel_in_op_achtergrond)
    Handler.indelen = indelen
    Handler.start_bewaking = staticmethod(start_bewaking)
    return Handler


def serveer(cfg: Config, store: Store, bronnen: dict, llm_factory) -> None:
    handler = maak_handler(cfg, store, bronnen, llm_factory)
    server = ThreadingHTTPServer((cfg.web_host, cfg.web_poort), handler)
    if cfg.meldingen_aan:
        handler.start_bewaking()
    print(f"attaché: http://{cfg.web_host}:{cfg.web_poort}  (stop with Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
