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

    def haal_op(self, sinds, maximum: int) -> list[Email]:
        mails = []
        for f in sorted(self.pad.glob("*.eml")):
            msg = email.message_from_bytes(f.read_bytes(), policy=email.policy.default)
            try:
                datum = parsedate_to_datetime(msg["Date"])
            except (TypeError, ValueError):
                datum = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if sinds and datum <= sinds:
                continue
            deel = msg.get_body(preferencelist=("plain", "html"))
            tekst = deel.get_content() if deel else ""
            if deel is not None and deel.get_content_type() == "text/html":
                tekst = re.sub(r"<[^>]+>", " ", tekst)
            mails.append(Email(bron=self.naam, bron_id=f.name, van=str(msg["From"] or ""),
                               aan=str(msg["To"] or ""), onderwerp=str(msg["Subject"] or ""),
                               datum=datum, tekst=tekst.strip(), thread_id=msg["Message-ID"]))
        return mails[:maximum]

    def maak_concept(self, bron_id: str, tekst: str, onderwerp: str, aan: str) -> str:
        self.uit.mkdir(parents=True, exist_ok=True)
        msg = email.message.EmailMessage()
        msg["To"] = aan
        msg["Subject"] = onderwerp if onderwerp.lower().startswith("re:") else f"Re: {onderwerp}"
        msg.set_content(tekst)
        doel = self.uit / f"concept-{Path(bron_id).stem}.eml"
        doel.write_bytes(bytes(msg))
        return f"opgeslagen als {doel}"
