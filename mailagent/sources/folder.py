"""Bron die .eml-bestanden uit een map leest. Bedoeld om te testen zonder echte mailbox."""

from __future__ import annotations

import email
import email.policy
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from ..models import Email


class MapBron:
    def __init__(self, naam: str, pad: Path, data_dir: Path):
        self.naam = naam
        self.pad = Path(pad)
        self.uit = Path(data_dir) / "concepten"
        self.vlaggen: dict[str, int] = {}   # bron_id -> kleur; alleen voor tests

    def haal_op(self, sinds, maximum: int, overslaan=None) -> list[Email]:
        mails = []
        for f in sorted(self.pad.glob("*.eml")):
            msg = email.message_from_bytes(f.read_bytes(), policy=email.policy.default)
            try:
                datum = parsedate_to_datetime(msg["Date"])
            except (TypeError, ValueError):
                datum = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if sinds and datum <= sinds:
                continue
            if overslaan and overslaan(f.name):
                continue
            deel = msg.get_body(preferencelist=("plain", "html"))
            tekst = deel.get_content() if deel else ""
            if deel is not None and deel.get_content_type() == "text/html":
                tekst = re.sub(r"<[^>]+>", " ", tekst)
            mails.append(Email(bron=self.naam, bron_id=f.name, van=str(msg["From"] or ""),
                               aan=str(msg["To"] or ""), onderwerp=str(msg["Subject"] or ""),
                               datum=datum, tekst=tekst.strip(), thread_id=msg["Message-ID"],
                               extra={"bulk": bool(msg["List-Unsubscribe"])
                                      or str(msg["Precedence"] or "").lower() in ("bulk", "list")}))
        mails.sort(key=lambda m: m.datum)
        self.laatste_totaal = len(mails)
        return mails[:maximum]

    def maak_concept(self, bron_id: str, tekst: str, onderwerp: str, aan: str, message_id: str | None = None) -> str:
        self.uit.mkdir(parents=True, exist_ok=True)
        msg = email.message.EmailMessage()
        msg["To"] = aan
        msg["Subject"] = onderwerp if onderwerp.lower().startswith("re:") else f"Re: {onderwerp}"
        msg.set_content(tekst)
        doel = self.uit / f"concept-{Path(bron_id).stem}.eml"
        doel.write_bytes(bytes(msg))
        return f"opgeslagen als {doel}"

    def verplaats(self, bron_ids: list[str], doel: str) -> dict[str, str | None]:
        doelmap = self.pad / (f"_map_{doel[4:].replace('/', '_')}" if doel.startswith("map:") else f"_{doel}")
        doelmap.mkdir(exist_ok=True)
        resultaat: dict[str, str | None] = {}
        for i in bron_ids:
            bron = self.pad / i
            if bron.exists():
                bron.rename(doelmap / i)
                resultaat[i] = None
            else:
                resultaat[i] = "bestand niet gevonden"
        return resultaat

    def markeer(self, bron_id: str, message_id: str | None, aan: bool, kleur: int = 4) -> None:
        if aan:
            self.vlaggen[bron_id] = kleur
        else:
            self.vlaggen.pop(bron_id, None)

    def mappenlijst(self) -> list[str]:
        return ["Projecten/ENTER÷IFITT", "ISTTE", "Reviews & tijdschriften", "BUas intern"]

    def terugzetten(self, message_ids: list[str], van: str) -> dict[str, str]:
        import email as _email
        map_ = self.pad / (f"_map_{van[4:].replace('/', '_')}" if van.startswith("map:") else f"_{van}")
        res = {}
        for f in map_.glob("*.eml") if map_.exists() else []:
            mid = _email.message_from_bytes(f.read_bytes())["Message-ID"]
            if mid in message_ids:
                f.rename(self.pad / f.name)
                res[mid] = f.name
        return {m: res.get(m, "FOUT:niet gevonden") for m in message_ids}

    def bron(self, bron_id: str, message_id: str | None) -> str:
        for f in [self.pad / bron_id, *self.pad.glob(f"_*/{bron_id}")]:
            if f.exists():
                return f.read_text(errors="replace")
        raise FileNotFoundError(bron_id)
