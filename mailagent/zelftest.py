"""Zelftest: kijkt na wat alleen op de Mac zelf te controleren is, zonder iets te verplaatsen of te versturen.

Gebruik: scripts/mailagent.sh zelftest  (of --zonder-claude om de proefvraag aan Claude over te slaan)

Het verslag noemt aantallen en tijden, geen afzenders, onderwerpen of inhoud: je kunt het zo doorsturen.
De proefvraag aan Claude gaat over een verzonnen mail."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

TEKEN = {"ok": "✓", "let": "!", "fout": "✗", "over": "–"}
NEP_MESSAGE_ID = "<zelftest-bestaat-niet@attache.local>"


@dataclass
class Verslag:
    regels: list[tuple[str, str, str]] = field(default_factory=list)   # (stand, onderdeel, tekst)
    uit: object = None                                                    # waar regels meteen naartoe gaan (print)

    def kop(self, tekst: str) -> None:
        self.regels.append(("kop", tekst, ""))
        if self.uit:
            self.uit(f"\n{tekst}")

    def zet(self, stand: str, onderdeel: str, tekst: str) -> None:
        self.regels.append((stand, onderdeel, tekst))
        if self.uit:
            self.uit(f"  {TEKEN[stand]} {onderdeel}: {tekst}")

    def tel(self) -> dict[str, int]:
        uit = {"ok": 0, "let": 0, "fout": 0, "over": 0}
        for stand, _, _ in self.regels:
            if stand in uit:
                uit[stand] += 1
        return uit

    def tekst(self) -> str:
        delen = []
        for stand, onderdeel, tekst in self.regels:
            delen.append(f"\n{onderdeel}" if stand == "kop" else f"  {TEKEN[stand]} {onderdeel}: {tekst}")
        t = self.tel()
        delen.append(f"\nSummary: {t['ok']} fine, {t['let']} to look at, {t['fout']} failed, {t['over']} skipped.")
        return "\n".join(delen).lstrip("\n") + "\n"


def _tijd(f, *args, **kw):
    t0 = time.time()
    uit = f(*args, **kw)
    return uit, time.time() - t0


def _kort(e: Exception, n: int = 240) -> str:
    return " ".join(str(e).split())[:n]


def zelftest(cfg, store, bronnen: dict, llm=None, met_claude: bool = True, uit=print,
             app_url: str = "http://127.0.0.1:8765/") -> Verslag:
    v = Verslag(uit=uit)

    # ---------- versie ----------
    v.kop("Version")
    repo = Path(__file__).resolve().parent.parent
    try:
        commit = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%h %cd", "--date=format:%d %b %H:%M"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        v.zet("ok" if commit else "let", "attaché", commit or "not a git checkout")
    except Exception as e:
        v.zet("let", "attaché", _kort(e))
    mac = platform.mac_ver()[0]
    v.zet("ok", "system", f"Python {sys.version.split()[0]}" + (f", macOS {mac}" if mac else f", {platform.system()}"))

    # ---------- instellingen ----------
    v.kop("Configuration")
    soorten = ", ".join(f"{n} ({type(b).__name__.replace('Bron', '').lower() or '?'})" for n, b in bronnen.items())
    v.zet("ok", "accounts", soorten or "none")
    v.zet("ok", "Claude", f"backend {cfg.backend}" + (f", model {cfg.claude_code_model}" if cfg.backend != "api" else f", model {cfg.model}"))
    v.zet("ok", "privacy", f"anonymising {'on' if cfg.anonimiseren_aan else 'OFF'}, local model {'on' if cfg.lokaal_aan else 'off'}")
    try:
        p = cfg.data_dir / ".zelftest"
        p.write_text("ok"); p.unlink()
        v.zet("ok", "data folder", "writable")
    except Exception as e:
        v.zet("fout", "data folder", _kort(e))

    # ---------- database ----------
    v.kop("Database")
    try:
        kolommen = {r[1] for r in store.db.execute("PRAGMA table_info(items)")}
        nodig = {"opgeruimd", "open_punten", "terug_in_inbox", "niet_in_inbox", "opgeborgen_in", "gelezen"}
        mist = sorted(nodig - kolommen)
        v.zet("fout" if mist else "ok", "schema", f"missing columns: {', '.join(mist)}" if mist else "up to date")
        n = lambda sql, *a: store.db.execute(sql, a).fetchone()[0]
        te_doen = n("SELECT COUNT(*) FROM items WHERE status IN ('wacht','fout','handmatig')")
        week = (datetime.now() - timedelta(days=7)).isoformat()
        v.zet("ok", "contents", f"{te_doen} in To do, {n('SELECT COUNT(*) FROM items WHERE afgehandeld >= ?', week)} handled this week, "
                                 f"{n('SELECT COUNT(*) FROM taken WHERE klaar = 0')} open tasks")
    except Exception as e:
        v.zet("fout", "database", _kort(e))

    # ---------- mail ----------
    nu = datetime.now().astimezone()
    for naam, bron in bronnen.items():
        soort = type(bron).__name__
        v.kop(f"Email: {naam}")
        if soort == "MapBron":
            v.zet("over", "source", "a test folder, not Apple Mail")
            continue
        if soort == "OutlookBron":
            cache = cfg.data_dir / ".token_cache.json"
            v.zet("ok" if cache.exists() else "fout", "Outlook", "signed in" if cache.exists() else "not signed in: run scripts/mailagent.sh login")
            continue
        rijen = []
        try:
            rijen, s = _tijd(bron.lijst, nu - timedelta(days=2))
            met_id = sum(1 for r in rijen if r[2])
            stand = "ok" if rijen and met_id == len(rijen) else "let"
            v.zet(stand, "inbox, last two days", f"{len(rijen)} emails, {met_id} with a Message-ID ({s:.1f} s)"
                  + ("" if rijen else "; check account: and mailbox: in config.yaml"))
        except Exception as e:
            v.zet("fout", "inbox", _kort(e))
        try:
            mails, s = _tijd(bron.haal_op, nu - timedelta(days=2), 1)
            if mails:
                m = mails[0]
                compleet = all([m.van, m.onderwerp is not None, m.tekst]) and bool(m.thread_id)
                v.zet("ok" if compleet else "let", "reading one email",
                      f"sender, subject, {len(m.tekst)} characters of text, Message-ID {'yes' if m.thread_id else 'no'} ({s:.1f} s)")
            else:
                v.zet("over", "reading one email", "no email in the last two days")
        except Exception as e:
            v.zet("fout", "reading one email", _kort(e))
        try:
            oud, s = _tijd(bron.lijst, nu - timedelta(days=60), nu - timedelta(days=30))
            v.zet("ok", "older email window", f"{len(oud)} emails between 60 and 30 days ago ({s:.1f} s)")
        except Exception as e:
            v.zet("fout", "older email window", _kort(e))
        if rijen:
            try:
                st, s = _tijd(bron.beantwoord, [(r[0], r[2] or None) for r in rijen[-5:]])
                tel = {}
                for waarde, _ in st.values():
                    tel[waarde] = tel.get(waarde, 0) + 1
                v.zet("ok" if st else "let", "replied to?", ", ".join(f"{k} {n_}" for k, n_ in sorted(tel.items())) + f" of {min(5, len(rijen))} checked ({s:.1f} s)")
            except Exception as e:
                v.zet("fout", "replied to?", _kort(e))
        try:
            mappen, s = _tijd(bron.mappenlijst)
            v.zet("ok", "folders", f"{len(mappen)} folders to file in ({s:.1f} s)")
        except Exception as e:
            v.zet("fout", "folders", _kort(e))
        try:
            from .sources.applemail import SCRIPT_MAPPEN, _osascript
            alle = {p.strip().strip("/").lower() for p in _osascript(SCRIPT_MAPPEN, bron.account).splitlines() if p.strip()}
            for sleutel, label in (("archief", "archive"), ("prullenbak", "bin")):
                pad = bron.mappen[sleutel]
                gevonden = pad.lower() in alle or any(a.split("/")[-1] == pad.lower().split("/")[-1] for a in alle)
                v.zet("ok" if gevonden else "fout", f"{label} folder", f"'{pad}' {'found' if gevonden else 'not found: set ' + sleutel + ': in config.yaml'}")
        except Exception as e:
            v.zet("fout", "archive and bin", _kort(e))
        try:
            verzonden, s = _tijd(bron.verzonden_sinds, nu - timedelta(days=1))
            v.zet("ok", "sent email", f"{len(verzonden)} in the last day, folder '{bron.mappen['verzonden']}' ({s:.1f} s)")
        except Exception as e:
            v.zet("fout", "sent email", _kort(e))
        # het zoeken in het archief op Message-ID: dit bepaalt hoe snel Done en terugzetten zijn
        t0 = time.time()
        try:
            bron.bron("0", NEP_MESSAGE_ID)
            v.zet("let", "archive search", "found an email that should not exist")
        except Exception:
            s = time.time() - t0
            v.zet("ok" if s < 10 else "let", "archive search", f"a full search of inbox and archive takes {s:.1f} s"
                  + ("" if s < 10 else "; slow, worth looking at (fout 9)"))

    # ---------- agenda ----------
    v.kop("Calendar")
    if not (cfg.agenda or {}).get("kalender"):
        v.zet("over", "calendar", "not set up")
    else:
        try:
            from .agenda import maak_agenda
            (ag, reden), s = _tijd(maak_agenda, cfg)
            if ag is None:
                v.zet("fout", "calendar", reden)
            else:
                vandaag = datetime.combine(nu.date(), datetime.min.time()).astimezone()
                lijst, s2 = _tijd(ag.afspraken, vandaag, vandaag + timedelta(days=1))
                v.zet("ok", "today", f"{len(lijst)} events ({s + s2:.1f} s)")
                try:
                    kal = [k for k in ag.kalenders() if k[1] == cfg.agenda.get("kalender")]
                    schrijf = any(k[2] for k in kal)
                    v.zet("ok" if schrijf else "fout", "your calendar",
                          f"'{cfg.agenda.get('kalender')}' {'found, writable' if schrijf else 'not found or read only'}")
                except Exception as e:
                    v.zet("let", "your calendar", _kort(e))
        except Exception as e:
            v.zet("fout", "calendar", _kort(e))

    # ---------- Claude ----------
    v.kop("Claude")
    if cfg.backend != "api":
        pad = shutil.which(cfg.claude_pad or "claude") or shutil.which("claude")
        if not pad:
            v.zet("fout", "claude", "not found on the PATH; is Claude Code installed?")
        else:
            try:
                versie = subprocess.run([pad, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
                v.zet("ok", "claude", versie or pad)
            except Exception as e:
                v.zet("let", "claude", _kort(e))
    if not met_claude:
        v.zet("over", "test question", "skipped (--zonder-claude)")
    else:
        try:
            from .models import Email, Sortering
            if llm is None:
                from .llm import maak_llm
                llm = maak_llm(cfg)
            project = "onderzoek" if "onderzoek" in cfg.projecten else (cfg.projecten[0] if cfg.projecten else "overig")
            mail = Email(bron="zelftest", bron_id="0", van="Editorial Office <editor@journal.example>", aan="you@example.org",
                         onderwerp="Invitation to review manuscript TT-0000", datum=nu, thread_id=NEP_MESSAGE_ID,
                         tekst=("Dear colleague,\n\nWe would like to invite you to review the manuscript TT-0000, "
                                "'Testing tourism flows', for the Journal of Examples. Please let us know by "
                                f"{(nu + timedelta(days=5)):%d %B %Y} whether you can accept, and return your report "
                                "within three weeks.\n\nKind regards,\nEditorial Office"))
            sortering = Sortering(project=project, actie="antwoorden", prioriteit="normaal",
                                  deadline=(nu + timedelta(days=5)).date().isoformat(), samenvatting="Review invitation",
                                  reden="self-test")
            u, s = _tijd(llm.werk_uit, mail, mail.tekst, sortering)
            punten = [p for p in (u.open_punten or []) if p.soort == "hele_mail"]
            met_taak = any(o.taak for p in punten for o in p.opties)
            wacht = not u.concept_antwoord and punten and punten[0].suggestie is None
            v.zet("ok" if u.concept_antwoord or wacht else "let", "draft",
                  f"{len(u.concept_antwoord or '')} characters ({s:.0f} s)" if u.concept_antwoord
                  else f"none yet: Claude waits for your choice, as your project instructions ask ({s:.0f} s)" if wacht
                  else f"empty ({s:.0f} s)")
            v.zet("ok" if punten else "let", "decision", f"{len(punten[0].opties)} choices: {', '.join(o.label for o in punten[0].opties)}" if punten
                  else "no Accept/Decline point; the draft works, the choices do not")
            v.zet("ok" if met_taak else "let", "task with a choice", "yes" if met_taak else "no option brought its own task")
        except Exception as e:
            v.zet("fout", "test question", _kort(e, 400))

    # ---------- lokaal model ----------
    v.kop("Local model")
    try:
        from .llm import OllamaLLM
        lokaal = OllamaLLM(cfg)
        modellen, s = _tijd(lokaal.modellen)
        if not cfg.lokaal_aan:
            v.zet("over", "Ollama", f"switched off in Settings ({len(modellen)} models installed)" if modellen else "switched off in Settings")
        elif not modellen:
            v.zet("fout", "Ollama", "running, but no model: ollama pull qwen3:8b")
        else:
            uitk, s2 = _tijd(lokaal.deel_afspraken_in, [{"nr": 1, "titel": "Werkcollege Test Y1", "organisator": "timetabling"}])
            v.zet("ok", "Ollama", f"{len(modellen)} models, model in use {cfg.lokaal_model or modellen[0]}, test answer in {s2:.0f} s")
    except Exception as e:
        v.zet("fout" if cfg.lokaal_aan else "over", "Ollama", ("not reachable: " if cfg.lokaal_aan else "not running (switched off, fine): ") + _kort(e, 120))

    # ---------- de app ----------
    v.kop("App")
    try:
        with urllib.request.urlopen(app_url, timeout=5) as r:
            v.zet("ok" if r.status == 200 else "fout", "page", f"{app_url} answers ({r.status})")
    except Exception as e:
        v.zet("fout", "page", f"{app_url} does not answer: {_kort(e, 120)}. Start it with launchctl kickstart -k gui/$(id -u)/nl.mailagent.inbox")
    if shutil.which("launchctl"):
        try:
            lijst = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
            for taak in ("nl.mailagent.inbox", "nl.mailagent.run"):
                regel = next((r for r in lijst.splitlines() if r.endswith(taak)), None)
                if not regel:
                    v.zet("fout", taak, "not loaded: run scripts/installeer-mac.sh")
                else:
                    pid, status, _ = (regel.split("\t") + ["", ""])[:3]
                    draait = pid.strip() not in ("-", "")
                    v.zet("ok" if draait or taak.endswith("run") else "fout", taak,
                          f"running (pid {pid.strip()})" if draait else f"loaded, last exit status {status.strip()}")
        except Exception as e:
            v.zet("let", "launchd", _kort(e))
    return v
