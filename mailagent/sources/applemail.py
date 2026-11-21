"""Bron voor Apple Mail via AppleScript (alleen macOS).

Werkt met elk account dat in Apple Mail staat, dus ook een apart ISTTE-adres of Gmail.
Bij goedkeuring opent Apple Mail een antwoordvenster met het concept erin. Je verstuurt zelf.
De eerste keer vraagt macOS of Terminal (of Python) Mail mag besturen: dat moet je toestaan.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta

from ..models import Email

RS, US = "\x1e", "\x1f"  # scheidingstekens tussen berichten en velden

SCRIPT_OPHALEN = r'''
on run argv
    set accountNaam to item 1 of argv
    set mailboxNaam to item 2 of argv
    set secondenTerug to (item 3 of argv) as integer
    set maximum to (item 4 of argv) as integer
    set RS to character id 30
    set US to character id 31
    set grens to (current date) - secondenTerug
    set uitvoer to ""
    tell application "Mail"
        set mb to mailbox mailboxNaam of account accountNaam
        set berichten to (messages of mb whose date received > grens)
        set teller to 0
        repeat with m in berichten
            if teller >= maximum then exit repeat
            set teller to teller + 1
            set ontvangers to ""
            repeat with r in to recipients of m
                set ontvangers to ontvangers & (address of r) & ", "
            end repeat
            set geleden to ((current date) - (date received of m)) as integer
            set uitvoer to uitvoer & (id of m as string) & US & (message id of m) & US & (sender of m) & US & ontvangers & US & (subject of m) & US & (geleden as string) & US & (content of m) & RS
        end repeat
    end tell
    return uitvoer
end run
'''

SCRIPT_CONCEPT = r'''
on run argv
    set accountNaam to item 1 of argv
    set mailboxNaam to item 2 of argv
    set berichtId to (item 3 of argv) as integer
    set conceptTekst to item 4 of argv
    tell application "Mail"
        set m to first message of mailbox mailboxNaam of account accountNaam whose id is berichtId
        set antwoord to reply m with opening window
        try
            set content of antwoord to conceptTekst & return & return & (content of antwoord)
            return "venster"
        on error
            set the clipboard to conceptTekst
            return "klembord"
        end try
    end tell
end run
'''


def _osascript(script: str, *args: str) -> str:
    r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"AppleScript-fout: {r.stderr.strip()}")
    return r.stdout


class AppleMailBron:
    def __init__(self, naam: str, account: str, mailbox: str = "Inbox"):
        self.naam, self.account, self.mailbox = naam, account, mailbox

    def haal_op(self, sinds, maximum: int) -> list[Email]:
        nu = datetime.now().astimezone()
        seconden = int((nu - sinds).total_seconds()) if sinds else 2 * 86400
        uit = _osascript(SCRIPT_OPHALEN, self.account, self.mailbox, str(seconden), str(maximum))
        mails = []
        for rec in uit.split(RS):
            velden = rec.split(US)
            if len(velden) < 7:
                continue
            mid, message_id, van, aan, onderwerp, geleden, inhoud = velden[:6] + [US.join(velden[6:])]
            mails.append(Email(bron=self.naam, bron_id=mid.strip(), van=van, aan=aan.rstrip(", "),
                               onderwerp=onderwerp, datum=nu - timedelta(seconds=int(geleden or 0)),
                               tekst=inhoud.strip(), thread_id=message_id))
        return mails

    def maak_concept(self, bron_id: str, tekst: str, onderwerp: str, aan: str) -> str:
        hoe = _osascript(SCRIPT_CONCEPT, self.account, self.mailbox, bron_id, tekst).strip()
        if hoe == "klembord":
            return "antwoordvenster geopend in Apple Mail; concepttekst staat op het klembord"
        return "antwoordvenster geopend in Apple Mail met het concept"
