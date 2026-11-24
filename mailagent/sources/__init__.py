"""Mailbronnen. Elke bron kan mail ophalen en een concept-antwoord klaarzetten. Geen enkele bron verstuurt mail."""

from __future__ import annotations

from typing import Protocol

from ..config import Config


class Bron(Protocol):
    naam: str

    def haal_op(self, sinds, maximum: int, overslaan=None) -> list: ...

    def maak_concept(self, bron_id: str, tekst: str, onderwerp: str, aan: str) -> str:
        """Zet een concept-antwoord klaar en geeft een korte beschrijving terug van waar het staat."""
        ...

    def verplaats(self, bron_ids: list[str], doel: str) -> dict[str, str | None]:
        """Verplaatst berichten naar 'archief' of 'prullenbak'. Per id: None bij succes, anders de fout."""
        ...


def maak_bronnen(cfg: Config) -> dict[str, Bron]:
    bronnen: dict[str, Bron] = {}
    for b in cfg.bronnen:
        soort, naam = b.get("type"), b.get("naam") or b.get("type")
        if soort == "map":
            from .folder import MapBron
            bronnen[naam] = MapBron(naam, cfg.pad.parent / b["pad"], cfg.data_dir)
        elif soort == "applemail":
            from .applemail import AppleMailBron
            bronnen[naam] = AppleMailBron(naam, b["account"], b.get("mailbox", "Inbox"),
                                          b.get("archief", "Archive"), b.get("prullenbak", "Deleted Items"),
                                          bool(b.get("allen_beantwoorden", False)), b.get("verzonden"))
        elif soort == "outlook":
            from .outlook import OutlookBron
            bronnen[naam] = OutlookBron(naam, b["client_id"], b.get("tenant", "organizations"),
                                        b.get("map", "inbox"), cfg.data_dir)
        else:
            raise SystemExit(f"Onbekend brontype in config: {soort!r}")
    if not bronnen:
        raise SystemExit("Geen bronnen ingesteld in config.yaml.")
    return bronnen
