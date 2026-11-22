"""Bron voor Apple Mail via AppleScript (alleen macOS).

Werkt met elk account dat in Apple Mail staat, dus ook een apart ISTTE-adres of Gmail.
Bij goedkeuring opent Apple Mail een antwoordvenster met het concept erin. Je verstuurt zelf.
De eerste keer vraagt macOS of Terminal (of Python) Mail mag besturen: dat moet je toestaan.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta

from ..models import Email

log = logging.getLogger("mailagent")
RS, US = "\x1e", "\x1f"  # scheidingstekens tussen berichten en velden

SCRIPT_LIJST = r'''
on run argv
    set accountNaam to item 1 of argv
    set mailboxNaam to item 2 of argv
    set secondenTerug to (item 3 of argv) as integer
    set RS to character id 30
    set US to character id 31
    set nu to current date
    set grens to nu - secondenTerug
    tell application "Mail"
        set mb to mailbox mailboxNaam of account accountNaam
        -- twee vragen voor alle berichten tegelijk; veel sneller dan per bericht
        repeat 3 times
            set ids to id of (every message of mb whose date received > grens)
            set datums to date received of (every message of mb whose date received > grens)
            if (count of ids) = (count of datums) then exit repeat
        end repeat
    end tell
    set uitvoer to ""
    repeat with i from 1 to count of ids
        set uitvoer to uitvoer & (item i of ids as string) & US & ((nu - (item i of datums)) as integer as string) & RS
    end repeat
    return uitvoer
end run
'''

SCRIPT_DETAILS = r'''
on run argv
    set accountNaam to item 1 of argv
    set mailboxNaam to item 2 of argv
    set secondenTerug to (item 3 of argv) as integer
    set gezocht to {}
    repeat with i from 4 to count of argv
        set end of gezocht to ((item i of argv) as integer)
    end repeat
    set RS to character id 30
    set US to character id 31
    set uitvoer to ""
    tell application "Mail"
        set mb to mailbox mailboxNaam of account accountNaam
        -- alleen binnen de berichten van de gekozen periode zoeken, niet in de hele inbox
        set berichten to (every message of mb whose date received > ((current date) - secondenTerug))
        repeat with m in berichten
            set mid to id of m
            if gezocht contains mid then
                set ontvangers to ""
                try
                    repeat with r in to recipients of m
                        set ontvangers to ontvangers & (address of r) & ", "
                    end repeat
                end try
                set geleden to ((current date) - (date received of m)) as integer
                set bulk to "0"
                try
                    if (count of (headers of m whose name is "List-Unsubscribe")) > 0 then set bulk to "1"
                end try
                try
                    set prec to content of (first header of m whose name is "Precedence")
                    if prec contains "bulk" or prec contains "list" then set bulk to "1"
                end try
                set uitvoer to uitvoer & (mid as string) & US & (message id of m) & US & (sender of m) & US & ontvangers & US & (subject of m) & US & (geleden as string) & US & bulk & US & (content of m) & RS
            end if
        end repeat
    end tell
    return uitvoer
end run
'''

HULP = r'''
on splits(tekst, teken)
    set oud to AppleScript's text item delimiters
    set AppleScript's text item delimiters to teken
    set delen to text items of tekst
    set AppleScript's text item delimiters to oud
    return delen
end splits

on zoekMap(accountNaam, pad)
    tell application "Mail"
        -- 1. een pad als "[Gmail]/Trash": stap voor stap door de submappen
        if pad contains "/" then
            try
                set delen to my splits(pad, "/")
                set huidig to mailbox (item 1 of delen) of account accountNaam
                repeat with i from 2 to count of delen
                    set huidig to mailbox (item i of delen) of huidig
                end repeat
                return huidig
            end try
            try
                return mailbox pad of account accountNaam
            end try
            set pad to last item of my splits(pad, "/")
        end if
        -- 2. zoeken op naam; bij meer dan één treffer gaat een Gmail-systeemmap voor
        set kandidaten to (every mailbox of account accountNaam whose name is pad)
        if (count of kandidaten) is 0 then error "map '" & pad & "' niet gevonden in account " & accountNaam
        repeat with k in kandidaten
            try
                set ouder to name of container of k
                if ouder starts with "[G" then return contents of k
            end try
        end repeat
        return item 1 of kandidaten
    end tell
end zoekMap

'''


SCRIPT_CONCEPT = HULP + r'''
on run argv
    set accountNaam to item 1 of argv
    set mailboxNaam to item 2 of argv
    set berichtId to (item 3 of argv) as integer
    set conceptTekst to item 4 of argv
    set berichtMid to item 5 of argv
    set archiefNaam to item 6 of argv
    set m to missing value
    tell application "Mail"
        try
            set m to first message of mailbox mailboxNaam of account accountNaam whose id is berichtId
        end try
    end tell
    -- al gearchiveerd? zoek in het archief op het Message-ID, dat bij verplaatsen gelijk blijft
    if m is missing value and berichtMid is not "" then
        try
            set arch to my zoekMap(accountNaam, archiefNaam)
            tell application "Mail" to set m to first message of arch whose message id is berichtMid
        end try
    end if
    if m is missing value then error "mail niet meer gevonden in " & mailboxNaam & " of " & archiefNaam
    tell application "Mail"
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


SCRIPT_MARKEER = HULP + r'''
on run argv
    set accountNaam to item 1 of argv
    set mailboxNaam to item 2 of argv
    set berichtId to (item 3 of argv) as integer
    set berichtMid to item 4 of argv
    set archiefNaam to item 5 of argv
    set aan to (item 6 of argv) is "1"
    set kleur to (item 7 of argv) as integer
    set m to missing value
    tell application "Mail"
        try
            set m to first message of mailbox mailboxNaam of account accountNaam whose id is berichtId
        end try
        if m is missing value and berichtMid is not "" then
            try
                set m to first message of mailbox mailboxNaam of account accountNaam whose message id is berichtMid
            end try
        end if
    end tell
    if m is missing value and berichtMid is not "" then
        try
            set arch to my zoekMap(accountNaam, archiefNaam)
            tell application "Mail" to set m to first message of arch whose message id is berichtMid
        end try
    end if
    if m is missing value then error "mail niet gevonden"
    tell application "Mail"
        if aan then
            set flag index of m to kleur
            set flagged status of m to true
        else
            set flagged status of m to false
        end if
    end tell
    return "ok"
end run
'''


SCRIPT_VERPLAATS = HULP + r'''
on run argv
    set accountNaam to item 1 of argv
    set mailboxNaam to item 2 of argv
    set doelNaam to item 3 of argv
    set uitvoer to ""
    set doel to my zoekMap(accountNaam, doelNaam)
    tell application "Mail"
        set mb to mailbox mailboxNaam of account accountNaam
        repeat with i from 4 to count of argv
            set berichtId to (item i of argv) as integer
            try
                set m to first message of mb whose id is berichtId
                set read status of m to true
                move m to doel
                set uitvoer to uitvoer & berichtId & ":ok" & linefeed
            on error foutTekst
                set uitvoer to uitvoer & berichtId & ":" & foutTekst & linefeed
            end try
        end repeat
    end tell
    return uitvoer
end run
'''


def _osascript(script: str, *args: str) -> str:
    try:
        r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Apple Mail reageerde 10 minuten niet. Staat er in Mail een venster of vraag open?")
    if r.returncode != 0:
        raise RuntimeError(f"AppleScript-fout: {r.stderr.strip()}")
    return r.stdout


class AppleMailBron:
    def __init__(self, naam: str, account: str, mailbox: str = "Inbox",
                 archief: str = "Archive", prullenbak: str = "Deleted Items"):
        self.naam, self.account, self.mailbox = naam, account, mailbox
        self.mappen = {"archief": archief, "prullenbak": prullenbak}

    def lijst(self, sinds) -> list[tuple[str, datetime]]:
        """Alle berichten sinds `sinds` als (id, datum), oudste eerst. Snel: zonder inhoud."""
        nu = datetime.now().astimezone()
        seconden = int((nu - sinds).total_seconds()) if sinds else 2 * 86400
        uit = _osascript(SCRIPT_LIJST, self.account, self.mailbox, str(seconden))
        rijen = []
        for rec in uit.split(RS):
            velden = rec.split(US)
            if len(velden) == 2 and velden[0].strip():
                rijen.append((velden[0].strip(), nu - timedelta(seconds=int(velden[1] or 0))))
        return sorted(rijen, key=lambda r: r[1])

    def haal_op(self, sinds, maximum: int, overslaan=None) -> list[Email]:
        """De oudste `maximum` nog niet verwerkte berichten sinds `sinds`."""
        self.laatste_rijen = self.lijst(sinds)
        rijen = [r for r in self.laatste_rijen if not (overslaan and overslaan(r[0]))]
        self.laatste_totaal = len(rijen)
        kies = [mid for mid, _ in rijen[:maximum]]
        if not kies:
            return []
        nu = datetime.now().astimezone()
        oudste = min(d for mid, d in rijen[:maximum])
        seconden = int((nu - oudste).total_seconds()) + 120
        mails = []
        stuk = 20   # in stukken, zodat je voortgang ziet en een trage mail niet alles ophoudt
        for i in range(0, len(kies), stuk):
            deel = kies[i:i + stuk]
            log.info("%s: inhoud ophalen %d-%d van %d…", self.naam, i + 1, i + len(deel), len(kies))
            uit = _osascript(SCRIPT_DETAILS, self.account, self.mailbox, str(seconden), *deel)
            for rec in uit.split(RS):
                velden = rec.split(US)
                if len(velden) < 8:
                    continue
                mid, message_id, van, aan, onderwerp, geleden, bulk, inhoud = velden[:7] + [US.join(velden[7:])]
                mails.append(Email(bron=self.naam, bron_id=mid.strip(), van=van, aan=aan.rstrip(", "),
                                   onderwerp=onderwerp, datum=nu - timedelta(seconds=int(geleden or 0)),
                                   tekst=inhoud.strip(), thread_id=message_id, extra={"bulk": bulk == "1"}))
        return sorted(mails, key=lambda m: m.datum)

    def maak_concept(self, bron_id: str, tekst: str, onderwerp: str, aan: str, message_id: str | None = None) -> str:
        hoe = _osascript(SCRIPT_CONCEPT, self.account, self.mailbox, bron_id, tekst,
                         message_id or "", self.mappen["archief"]).strip()
        if hoe == "klembord":
            return "antwoordvenster geopend in Apple Mail; concepttekst staat op het klembord"
        return "antwoordvenster geopend in Apple Mail met het concept"

    def markeer(self, bron_id: str, message_id: str | None, aan: bool, kleur: int = 4) -> None:
        """Vlag zetten (aan) of weghalen. Kleur: 0 rood, 1 oranje, 2 geel, 3 groen, 4 blauw, 5 paars, 6 grijs."""
        _osascript(SCRIPT_MARKEER, self.account, self.mailbox, bron_id, message_id or "",
                   self.mappen["archief"], "1" if aan else "0", str(kleur))

    def verplaats(self, bron_ids: list[str], doel: str) -> dict[str, str | None]:
        """Verplaatst berichten naar de archief- of prullenbakmap. Niets wordt definitief verwijderd."""
        uit = _osascript(SCRIPT_VERPLAATS, self.account, self.mailbox, self.mappen[doel], *bron_ids)
        resultaat: dict[str, str | None] = {i: "geen antwoord van Apple Mail" for i in bron_ids}
        for regel in uit.splitlines():
            mid, _, status = regel.partition(":")
            if mid.strip() in resultaat:
                resultaat[mid.strip()] = None if status.strip() == "ok" else status.strip()
        return resultaat
