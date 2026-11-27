"""Opdrachtregel: mailagent run | inbox | overzicht | login."""

from __future__ import annotations

import argparse
import logging
import sys

from . import config as config_mod
from .store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mailagent", description="attaché: email workflow with Claude. Never sends email by itself.")
    ap.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    sub = ap.add_subparsers(dest="opdracht", required=True)
    rp = sub.add_parser("run", help="get new mail, sort it and prepare drafts")
    rp.add_argument("--dagen", type=int, help="look back this many days in this round instead of terugkijken_dagen")
    rp.add_argument("--max", type=int, help="maximum number of emails per account in this round")
    rp.add_argument("--model", help="a different Claude model for this round, for example sonnet")
    sub.add_parser("inbox", help="start the app page on your own Mac")
    ov = sub.add_parser("overzicht", help="show an overview per project (Markdown)")
    ov.add_argument("--uren", type=int, default=24)
    ov.add_argument("--bestand", help="write the overview to this file")
    sub.add_parser("login", help="sign in to Outlook once (Microsoft 365)")
    sub.add_parser("rondes-installeren", help="install the scheduled mail rounds from config.yaml in launchd")
    sub.add_parser("agendas", help="list the calendars on this Mac, for agenda: in config.yaml")
    sub.add_parser("agenda-test", help="create a test event to check the reminder and lock in Outlook")
    tb = sub.add_parser("test-bronnen", help="show email from the last 24 hours, without Claude and without saving anything")
    tb.add_argument("--uren", type=int, default=24)
    vb = sub.add_parser("vergeet-bron", help="remove everything attaché stored from an account that is no longer in config.yaml")
    vb.add_argument("naam", help="the account's naam: from config.yaml, for example google")
    vb.add_argument("--ja", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config_mod.laad(args.config)
    store = Store(cfg.db_pad)

    if args.opdracht == "vergeet-bron":
        if any(b.get("naam") == args.naam for b in cfg.bronnen):
            print(f"'{args.naam}' is still in config.yaml under bronnen:. Remove it there first, then run this again.")
            return 1
        store = Store(cfg.db_pad)
        n = store.db.execute("SELECT COUNT(*) FROM mails WHERE bron=?", (args.naam,)).fetchone()[0]
        if not n:
            print(f"attaché has no email stored from '{args.naam}'.")
            return 0
        if not args.ja and input(f"Remove {n} emails from '{args.naam}' from attaché? Apple Mail is not touched. [y/N] ").strip().lower() not in ("y", "yes", "j", "ja"):
            print("Nothing removed.")
            return 0
        tel = store.vergeet_bron(args.naam)
        print(f"Removed {tel['mails']} emails and {tel['items']} items from '{args.naam}'."
              + (f" {tel['taken_losgekoppeld']} tasks stay, without the email." if tel.get("taken_losgekoppeld") else ""))
        return 0
    if args.opdracht == "overzicht":
        from .pipeline import overzicht
        tekst = overzicht(cfg, store, args.uren)
        if args.bestand:
            open(args.bestand, "w", encoding="utf-8").write(tekst)
        print(tekst)
        return 0

    if args.opdracht == "rondes-installeren":
        import plistlib
        import subprocess
        from pathlib import Path
        from .agenda import lees_werktijd
        werk = lees_werktijd(cfg.agenda)
        tijden = [{"Weekday": d % 7, "Hour": int(r.split(":")[0]), "Minute": int(r.split(":")[1])}
                  for d in sorted(werk.dagen) for r in cfg.rondes]
        plist = Path.home() / "Library/LaunchAgents/nl.mailagent.run.plist"
        if not plist.exists():
            print(f"{plist} does not exist; run scripts/installeer-mac.sh first")
            return 1
        data = plistlib.loads(plist.read_bytes())
        data["StartCalendarInterval"] = tijden
        plist.write_bytes(plistlib.dumps(data))
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
        print(f"Mail rounds set: {', '.join(cfg.rondes)} on working days")
        return 0

    if args.opdracht == "agendas":
        from .agenda import AppleAgenda
        try:
            ag = AppleAgenda(kalender="")
        except ImportError:
            print("Install first: .venv/bin/pip install pyobjc-framework-EventKit")
            return 1
        for account, naam, schrijfbaar in sorted(ag.kalenders()):
            print(f"  account: {account!r:28} kalender: {naam!r}" + ("" if schrijfbaar else "   (read only)"))
        return 0

    if args.opdracht == "agenda-test":
        from .agenda import maak_agenda, nl_moment
        ag, fout = maak_agenda(cfg)
        if ag is None:
            print(fout)
            return 1
        ev, info = ag.test_afspraak()
        print(f"Test event created: tomorrow {nl_moment(info['start'])}, 'attaché test: lock and reminder'.")
        print(f"  reminder: {'yes, ' + str(ag.herinnering_minuten) + ' min before' if info['herinneringen'] else 'no'}")
        print(f"  private: {'yes' if info['slot'] else 'no, this calendar does not allow it'}"
              + (f" (level {info['niveau']})" if info.get("niveau") else ""))
        print("Check in Outlook (also on the web or your phone) that the event has a lock and a reminder.")
        try:
            input("Press Enter to delete the test event again…")
        except EOFError:
            pass
        ag.verwijder(ev)
        print("Test event deleted.")
        return 0

    from .sources import maak_bronnen
    bronnen = maak_bronnen(cfg)

    if args.opdracht == "login":
        for naam, bron in bronnen.items():
            if hasattr(bron, "login"):
                print(f"Signing in for account '{naam}':")
                bron.login()
        return 0

    if args.opdracht == "test-bronnen":
        from datetime import datetime, timedelta
        sinds = datetime.now().astimezone() - timedelta(hours=args.uren)
        fout = False
        import time
        for naam, bron in bronnen.items():
            print(f"{naam}: searching Apple Mail…", flush=True)
            t0 = time.time()
            try:
                mails = bron.haal_op(sinds, 10)
            except Exception as ex:
                print(f"{naam}: FAILED: {ex}")
                fout = True
                continue
            totaal = getattr(bron, "laatste_totaal", len(mails))
            rijen = getattr(bron, "laatste_rijen", None)
            nog = sum(1 for r in rijen if not store.is_bekend_sleutel(f"{naam}:{r[0]}")) if rijen is not None else None
            print(f"{naam}: {totaal} " + ("email" if totaal == 1 else "emails") + f" in the last {args.uren} hours"
                  + (f", of which {nog} not yet processed" if nog is not None else "")
                  + f" (oudste 10 getoond, {time.time() - t0:.0f} s)")
            for m in mails:
                print(f"  {m.datum:%d %b %H:%M}  {m.van[:40]:40}  {m.onderwerp[:60]}")
        return 1 if fout else 0

    from .llm import maak_llm
    from .config import pas_instellingen_toe
    pas_instellingen_toe(cfg, store)   # de keuzes uit het menu Settings

    if args.opdracht == "run":
        from .pipeline import RondeBezig, overzicht, run
        if args.model:
            cfg.claude_code_model = cfg.model = args.model
        rest: dict = {}
        try:
            res = run(cfg, store, bronnen, maak_llm(cfg), dagen=args.dagen, maximum=args.max, voortgang=rest)
        except RondeBezig as e:
            print(e)
            return 0
        for naam, n in res.items():
            print(f"{naam}: " + ("getting mail failed (see the log)" if n < 0 else f"{n} new " + ("email" if n == 1 else "emails") + " processed"
                                 + (f", {rest[naam]} still to go" if rest.get(naam) else "")))
        ochtendmelding(cfg, store)
        (cfg.data_dir / "overzicht.md").write_text(overzicht(cfg, store), encoding="utf-8")
        return 1 if any(n < 0 for n in res.values()) else 0

    if args.opdracht == "inbox":
        from .web import serveer
        serveer(cfg, store, bronnen, lambda: maak_llm(cfg))
        return 0
    return 0


def ochtendmelding(cfg, store) -> None:
    """Na de eerste ronde van de dag (vóór 11:00): één melding met je dag. Eén keer per dag."""
    from datetime import datetime
    from .agenda import maak_agenda
    from .melding import meld
    from .pipeline import ochtend, ochtend_tekst
    nu = datetime.now()
    if not cfg.ochtend_aan or nu.hour >= 11 or nu.isoweekday() > 5:
        return
    sleutel = f"ochtend:{nu.date().isoformat()}"
    if store.is_gemeld(sleutel):
        return
    ag, _ = maak_agenda(cfg)
    meld("Your day", ochtend_tekst(ochtend(cfg, store, ag)))
    store.zet_gemeld(sleutel)


if __name__ == "__main__":
    sys.exit(main())
