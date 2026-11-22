"""Opdrachtregel: mailagent run | inbox | overzicht | login."""

from __future__ import annotations

import argparse
import logging
import sys

from . import config as config_mod
from .store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mailagent", description="E-mailworkflow met Claude. Verstuurt nooit zelf mail.")
    ap.add_argument("-c", "--config", default="config.yaml", help="pad naar config.yaml")
    sub = ap.add_subparsers(dest="opdracht", required=True)
    rp = sub.add_parser("run", help="nieuwe mail ophalen, sorteren en concepten voorbereiden")
    rp.add_argument("--dagen", type=int, help="eenmalig zoveel dagen terugkijken (inhaalronde)")
    rp.add_argument("--max", type=int, help="maximaal aantal mails per account in deze ronde")
    rp.add_argument("--model", help="ander Claude-model voor deze ronde, bijvoorbeeld sonnet")
    sub.add_parser("inbox", help="goedkeuringspagina starten op je eigen Mac")
    ov = sub.add_parser("overzicht", help="overzicht per project tonen (Markdown)")
    ov.add_argument("--uren", type=int, default=24)
    ov.add_argument("--bestand", help="schrijf het overzicht naar dit bestand")
    sub.add_parser("login", help="eenmalig inloggen bij Outlook (Microsoft 365)")
    sub.add_parser("agendas", help="agenda's op deze Mac tonen, voor agenda: in config.yaml")
    tb = sub.add_parser("test-bronnen", help="mail van het afgelopen etmaal tonen, zonder Claude en zonder iets op te slaan")
    tb.add_argument("--uren", type=int, default=24)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config_mod.laad(args.config)
    store = Store(cfg.db_pad)

    if args.opdracht == "overzicht":
        from .pipeline import overzicht
        tekst = overzicht(cfg, store, args.uren)
        if args.bestand:
            open(args.bestand, "w", encoding="utf-8").write(tekst)
        print(tekst)
        return 0

    if args.opdracht == "agendas":
        from .agenda import AppleAgenda
        try:
            ag = AppleAgenda(kalender="")
        except ImportError:
            print("Installeer eerst: .venv/bin/pip install pyobjc-framework-EventKit")
            return 1
        for account, naam, schrijfbaar in sorted(ag.kalenders()):
            print(f"  account: {account!r:28} kalender: {naam!r}" + ("" if schrijfbaar else "   (alleen lezen)"))
        return 0

    from .sources import maak_bronnen
    bronnen = maak_bronnen(cfg)

    if args.opdracht == "login":
        for naam, bron in bronnen.items():
            if hasattr(bron, "login"):
                print(f"Inloggen voor bron '{naam}':")
                bron.login()
        return 0

    if args.opdracht == "test-bronnen":
        from datetime import datetime, timedelta
        sinds = datetime.now().astimezone() - timedelta(hours=args.uren)
        fout = False
        for naam, bron in bronnen.items():
            try:
                mails = bron.haal_op(sinds, 10)
            except Exception as ex:
                print(f"{naam}: MISLUKT: {ex}")
                fout = True
                continue
            totaal = getattr(bron, "laatste_totaal", len(mails))
            nog = sum(1 for r in bron.lijst(sinds) if not store.is_bekend_sleutel(f"{naam}:{r[0]}")) if hasattr(bron, "lijst") else None
            print(f"{naam}: {totaal} mail(s) in de afgelopen {args.uren} uur"
                  + (f", waarvan {nog} nog niet verwerkt" if nog is not None else "") + " (oudste 10 getoond)")
            for m in mails:
                print(f"  {m.datum:%d-%m %H:%M}  {m.van[:40]:40}  {m.onderwerp[:60]}")
        return 1 if fout else 0

    from .llm import maak_llm

    if args.opdracht == "run":
        from .pipeline import overzicht, run
        if args.model:
            cfg.claude_code_model = cfg.model = args.model
        res = run(cfg, store, bronnen, maak_llm(cfg), dagen=args.dagen, maximum=args.max)
        for naam, n in res.items():
            print(f"{naam}: {'ophalen mislukt (zie log)' if n < 0 else f'{n} nieuwe mail(s) verwerkt'}")
        (cfg.data_dir / "overzicht.md").write_text(overzicht(cfg, store), encoding="utf-8")
        return 1 if any(n < 0 for n in res.values()) else 0

    if args.opdracht == "inbox":
        from .web import serveer
        serveer(cfg, store, bronnen, lambda: maak_llm(cfg))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
