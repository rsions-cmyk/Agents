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
                -- waarop deze mail antwoordt: om 'wacht op antwoord' af te sluiten
                set verw to ""
                try
                    set verw to content of (first header of m whose name is "In-Reply-To")
                end try
                try
                    set verw to verw & " " & (content of (first header of m whose name is "References"))
                end try
                set uitvoer to uitvoer & (mid as string) & US & (message id of m) & US & (sender of m) & US & ontvangers & US & (subject of m) & US & (geleden as string) & US & bulk & US & verw & US & (content of m) & RS
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


def titelstuk(onderwerp: str) -> str:
    """Stuk van het onderwerp waaraan je het antwoordvenster herkent: zonder Re:, AW:, Antw: of Fw: ervoor,
    want Mail zet daar zelf 'Re: ' voor (en voegt ze soms samen)."""
    import re
    kaal = re.sub(r"^\s*((re|aw|antw|fw|fwd|wg|doorst|sv|vs)\s*(\[\d+\])?\s*:\s*)+", "", onderwerp or "", flags=re.IGNORECASE).strip()
    return kaal[:25]


def controlestuk(tekst: str) -> str:
    """Een herkenbaar stuk van het concept (begin van de langste regel) om in het antwoord terug te vinden.
    Niet de aanhef: 'Beste Jan,' staat ook in andere antwoorden."""
    regels = [r.strip() for r in tekst.splitlines() if r.strip()]
    return max(regels, key=len)[:60] if regels else ""


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
    if m is missing value and berichtMid is not "" then
        try
            tell application "Mail" to set m to first message of mailbox mailboxNaam of account accountNaam whose message id is berichtMid
        end try
    end if
    if m is missing value then error "mail niet meer gevonden in " & mailboxNaam & " of " & archiefNaam
    set allen to (count of argv) > 6 and (item 7 of argv) is "1"
    set verzend to (count of argv) > 7 and (item 8 of argv) is "1"
    -- 1. Een echt antwoordvenster: Mail zet de oorspronkelijke mail er zelf in, met alle opmaak,
    --    en het bericht hoort bij het gesprek. De inhoud zetten via AppleScript werkt niet (venster wordt leeg),
    --    dus plakken we de concepttekst bovenaan.
    set oudKlembord to missing value
    try
        set oudKlembord to the clipboard
    end try
    set the clipboard to conceptTekst
    -- hoeveel vensters Mail nu heeft, om straks het nieuwe antwoordvenster te herkennen
    set voor to -1
    try
        tell application "System Events" to set voor to count of windows of process "Mail"
    end try
    tell application "Mail"
        activate
        if allen then
            set antwoord to reply m with opening window and reply to all
        else
            set antwoord to reply m with opening window
        end if
    end tell
    -- 2. Wachten (tot 9 seconden; Exchange kan traag zijn) tot het nieuwe antwoordvenster er is en
    --    vooraan staat. Pas dan plakken, zodat je tekst nooit in een ander venster belandt.
    set fragment to item 10 of argv
    set klaar to false
    set titel to ""
    try
        tell application "System Events"
            tell process "Mail"
                repeat with poging from 1 to 30
                    set frontmost to true
                    try
                        set titel to name of front window
                        set nieuw to (voor < 0) or ((count of windows) > voor)
                        -- na ruim 4 seconden ook zonder extra venster (Mail opent antwoorden soms als tabblad)
                        if fragment is "" then
                            if nieuw then set klaar to true
                        else if titel contains fragment and (nieuw or poging > 14) then
                            set klaar to true
                        end if
                        if klaar then exit repeat
                    end try
                    delay 0.3
                end repeat
            end tell
            if klaar then
                delay 0.4
                keystroke "v" using command down
            end if
        end tell
    on error foutTekst
        return "geen-toegang" & tab & foutTekst
    end try
    if not klaar then return "klembord" & tab & titel & tab & fragment
    delay 0.8
    -- 3. Controleren in DIT antwoord (het bericht dat reply teruggaf, niet een ander open venster)
    --    of je tekst erin staat. Lukt dat niet, dan blijft je tekst op het klembord staan.
    set controle to item 9 of argv
    set gecontroleerd to false
    try
        tell application "Mail" to set gecontroleerd to ((content of antwoord) contains controle)
    end try
    if not gecontroleerd then return "niet-gecontroleerd"
    set uitkomst to "geplakt"
    if verzend then
        set uitkomst to "niet-verzonden"
        try
            tell application "Mail" to send antwoord
            set uitkomst to "verzonden"
        end try
    end if
    if oudKlembord is not missing value then
        try
            set the clipboard to oudKlembord
        end try
    end if
    return uitkomst
end run
'''


SCRIPT_RECENT = r'''
on run argv
    set accountNaam to item 1 of argv
    set mailboxNaam to item 2 of argv
    set secondenTerug to (item 3 of argv) as integer
    set RS to character id 30
    set US to character id 31
    set uitvoer to ""
    tell application "Mail"
        set mb to mailbox mailboxNaam of account accountNaam
        repeat with m in (every message of mb whose date received > ((current date) - secondenTerug))
            try
                set uitvoer to uitvoer & (id of m as string) & US & (sender of m) & US & (subject of m) & RS
            end try
        end repeat
    end tell
    return uitvoer
end run
'''

SCRIPT_VERZONDEN = HULP + r'''
on run argv
    set accountNaam to item 1 of argv
    set mapNaam to item 2 of argv
    set secondenTerug to (item 3 of argv) as integer
    set RS to character id 30
    set US to character id 31
    set uitvoer to ""
    set mb to my zoekMap(accountNaam, mapNaam)
    tell application "Mail"
        set berichten to (every message of mb whose date sent > ((current date) - secondenTerug))
        repeat with m in berichten
            try
                with timeout of 30 seconds
                    set ontvangers to ""
                    repeat with r in to recipients of m
                        set ontvangers to ontvangers & (address of r) & ", "
                    end repeat
                    repeat with r in cc recipients of m
                        set ontvangers to ontvangers & (address of r) & ", "
                    end repeat
                    set geleden to ((current date) - (date sent of m)) as integer
                    set antwoordOp to ""
                    try
                        set antwoordOp to content of (first header of m whose name is "In-Reply-To")
                    end try
                    set verwijzingen to ""
                    try
                        set verwijzingen to content of (first header of m whose name is "References")
                    end try
                    set inhoud to content of m
                    if (length of inhoud) > 4000 then set inhoud to text 1 thru 4000 of inhoud
                    set uitvoer to uitvoer & (message id of m) & US & ontvangers & US & (subject of m) & US & (geleden as string) & US & antwoordOp & US & verwijzingen & US & inhoud & RS
                end timeout
            end try
        end repeat
    end tell
    return uitvoer
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


SCRIPT_TERUG = HULP + r'''
on run argv
    set accountNaam to item 1 of argv
    set inboxNaam to item 2 of argv
    set vanNaam to item 3 of argv
    set uitvoer to ""
    set van to my zoekMap(accountNaam, vanNaam)
    tell application "Mail"
        set inbox to mailbox inboxNaam of account accountNaam
        repeat with i from 4 to count of argv
            set mid to item i of argv
            try
                set m to first message of van whose message id is mid
                move m to inbox
                delay 0.3
                set nieuw to first message of inbox whose message id is mid
                set uitvoer to uitvoer & mid & tab & (id of nieuw as string) & linefeed
            on error foutTekst
                set uitvoer to uitvoer & mid & tab & "FOUT:" & foutTekst & linefeed
            end try
        end repeat
    end tell
    return uitvoer
end run
'''


SCRIPT_BRON = HULP + r'''
on run argv
    set accountNaam to item 1 of argv
    set mailboxNaam to item 2 of argv
    set berichtId to (item 3 of argv) as integer
    set berichtMid to item 4 of argv
    set archiefNaam to item 5 of argv
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
    tell application "Mail" to return source of m
end run
'''


SCRIPT_MAPPEN = r'''
on paden(mb, voorvoegsel, diepte)
    tell application "Mail" to set naam to name of mb
    set pad to voorvoegsel & naam
    set uit to pad & linefeed
    if diepte < 6 then
        try
            tell application "Mail" to set kinderen to every mailbox of mb
            repeat with k in kinderen
                set uit to uit & my paden(k, pad & "/", diepte + 1)
            end repeat
        end try
    end if
    return uit
end paden

on run argv
    set accountNaam to item 1 of argv
    set uitvoer to ""
    tell application "Mail" to set alle to every mailbox of account accountNaam
    repeat with mb in alle
        -- vanaf de mappen bovenaan, met al hun submappen (ook die in je Inbox)
        set bovenaan to true
        try
            tell application "Mail"
                if class of (container of mb) is mailbox then set bovenaan to false
            end tell
        end try
        if bovenaan then set uitvoer to uitvoer & my paden(mb, "", 1)
    end repeat
    return uitvoer
end run
'''

SCRIPT_MAP_MAKEN = HULP + r'''
on run argv
    set accountNaam to item 1 of argv
    set pad to item 2 of argv
    tell application "Mail"
        tell account accountNaam to make new mailbox with properties {name:pad}
    end tell
    delay 1
    -- controleren dat hij er echt staat
    set m to my zoekMap(accountNaam, pad)
    return "ok"
end run
'''

SYSTEEMMAPPEN = {"inbox", "postvak in", "postvak uit", "ongewenste e-mail", "verzonden",
                 "outbox", "drafts", "concepten", "sent", "sent items", "sent mail", "verzonden items",
                 "deleted items", "verwijderde items", "trash", "junk", "junk email", "spam", "conflicts",
                 "local failures", "server failures", "sync issues", "rss feeds", "rss subscriptions",
                 "rss-abonnementen", "conversation history", "snoozed", "gepland", "notes", "tasks", "journal",
                 "calendar", "contacts", "all mail", "important", "starred", "archive", "outbox"}


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
                 archief: str = "Archive", prullenbak: str = "Deleted Items", allen_beantwoorden: bool = False,
                 verzonden: str | None = None):
        self.naam, self.account, self.mailbox = naam, account, mailbox
        self.allen_beantwoorden = allen_beantwoorden
        if verzonden is None:   # Gmail of Exchange, af te leiden uit de andere mappen
            verzonden = "[Gmail]/Sent Mail" if archief.startswith("[Gmail]") or prullenbak.startswith("[Gmail]") else "Sent Items"
        self.mappen = {"archief": archief, "prullenbak": prullenbak, "verzonden": verzonden}

    def recente(self, seconden: int) -> list[tuple[str, str, str]]:
        """Snel, zonder inhoud: (id, afzender, onderwerp) van mail die de laatste `seconden` binnenkwam."""
        uit = _osascript(SCRIPT_RECENT, self.account, self.mailbox, str(int(seconden)))
        rijen = []
        for rec in uit.split(RS):
            velden = rec.split(US)
            if len(velden) >= 3:
                rijen.append((velden[0].strip(), velden[1], US.join(velden[2:]).strip()))
        return rijen

    def verzonden_sinds(self, sinds: datetime) -> list[dict]:
        """Mail die je zelf verstuurde sinds `sinds`, vanaf elk apparaat (de map Verzonden synchroniseert)."""
        nu = datetime.now().astimezone()
        seconden = max(60, int((nu - sinds).total_seconds()))
        uit = _osascript(SCRIPT_VERZONDEN, self.account, self.mappen["verzonden"], str(seconden))
        berichten = []
        for rec in uit.split(RS):
            velden = rec.split(US)
            if len(velden) < 7:
                continue
            mid, aan, onderwerp, geleden, antwoord_op, verwijzingen = velden[:6]
            berichten.append({"message_id": mid.strip(), "aan": aan.lower(), "onderwerp": onderwerp,
                              "datum": nu - timedelta(seconds=int(geleden or 0)),
                              "in_reply_to": antwoord_op, "references": verwijzingen,
                              "tekst": US.join(velden[6:]).strip()})
        return berichten

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
                if len(velden) < 9:
                    continue
                mid, message_id, van, aan, onderwerp, geleden, bulk, verw, inhoud = velden[:8] + [US.join(velden[8:])]
                mails.append(Email(bron=self.naam, bron_id=mid.strip(), van=van, aan=aan.rstrip(", "),
                                   onderwerp=onderwerp, datum=nu - timedelta(seconds=int(geleden or 0)),
                                   tekst=inhoud.strip(), thread_id=message_id,
                                   extra={"bulk": bulk == "1", "verwijzingen": verw.strip()}))
        return sorted(mails, key=lambda m: m.datum)

    def maak_concept(self, bron_id: str, tekst: str, onderwerp: str, aan: str, message_id: str | None = None,
                     verzenden: bool = False) -> str:
        uit = _osascript(SCRIPT_CONCEPT, self.account, self.mailbox, bron_id, tekst, message_id or "",
                         self.mappen["archief"], "1" if self.allen_beantwoorden else "0", "1" if verzenden else "0",
                         controlestuk(tekst), titelstuk(onderwerp)).strip()
        hoe, _, details = uit.partition("\t")
        if hoe != "geplakt" and hoe != "verzonden":
            log.warning("antwoord in Mail: %s (%s)", hoe, details.replace("\t", " | "))
        self.laatste_uitkomst = hoe
        if hoe == "verzonden":
            return "verzonden"
        if hoe == "niet-verzonden":
            return "niet verzonden: Mail weigerde te versturen; het antwoord staat open in Mail"
        if hoe == "niet-gecontroleerd":
            return ("ik kon niet controleren of je tekst in het nieuwe antwoord staat"
                    + (", dus er is niets verstuurd" if verzenden else "")
                    + ". Kijk in Mail; staat hij er niet, plak dan met ⌘V. Je tekst staat op het klembord")
        if hoe == "geplakt":
            return "antwoord staat klaar in Apple Mail, met je tekst boven de oorspronkelijke mail; versturen doe je zelf"
        if hoe == "geen-toegang":
            return ("Antwoord geopend, maar plakken mag nog niet. Je tekst staat op het klembord: klik in het antwoord "
                    "en druk op ⌘V. Zelf plakken lukt na toestemming in Systeeminstellingen, Privacy en beveiliging, "
                    "Toegankelijkheid (zet daar Terminal of Python aan)")
        return ("Antwoord geopend, maar het venster kwam niet op tijd vooraan, dus ik heb niet geplakt. Je tekst staat "
                "op het klembord: klik in het antwoord en druk op ⌘V")

    def markeer(self, bron_id: str, message_id: str | None, aan: bool, kleur: int = 4) -> None:
        """Vlag zetten (aan) of weghalen. Kleur: 0 rood, 1 oranje, 2 geel, 3 groen, 4 blauw, 5 paars, 6 grijs."""
        _osascript(SCRIPT_MARKEER, self.account, self.mailbox, bron_id, message_id or "",
                   self.mappen["archief"], "1" if aan else "0", str(kleur))

    def terugzetten(self, message_ids: list[str], van: str) -> dict[str, str]:
        """Mail terug naar de inbox (ongedaan maken). van: 'archief', 'prullenbak' of 'map:<pad>'.
        Geeft per Message-ID het nieuwe id in de inbox, of 'FOUT:...'."""
        map_ = van[4:] if van.startswith("map:") else self.mappen[van]
        uit = _osascript(SCRIPT_TERUG, self.account, self.mailbox, map_, *message_ids)
        res = {}
        for regel in uit.splitlines():
            mid, _, nieuw = regel.partition("\t")
            if mid:
                res[mid] = nieuw.strip()
        return res

    def bron(self, bron_id: str, message_id: str | None) -> str:
        """De volledige mail (MIME-bron), voor de opgemaakte weergave."""
        return _osascript(SCRIPT_BRON, self.account, self.mailbox, bron_id, message_id or "", self.mappen["archief"])

    def mappenlijst(self) -> list[str]:
        """Mappen waar je mail in kunt opbergen, als pad: eerst die in je Inbox ('Inbox/ISTTE'), dan de rest,
        submappen van het archief als laatste. Systeemmappen tellen alleen bovenaan mee, zodat een eigen
        submap die toevallig 'Archive' of 'Tasks' heet gewoon in de lijst staat."""
        uit = _osascript(SCRIPT_MAPPEN, self.account)
        inbox = self.mailbox.lower()
        archief = self.mappen["archief"].split("/")[-1].lower()
        weg_bovenaan = SYSTEEMMAPPEN | {inbox, archief, self.mappen["prullenbak"].split("/")[-1].lower()}
        nooit_onder = {self.mappen["prullenbak"].split("/")[-1].lower(), "deleted items", "verwijderde items", "trash",
                       "junk", "junk email", "ongewenste e-mail", "spam", "sent items", "verzonden items", "sent",
                       "drafts", "concepten", "outbox", "postvak uit", "conversation history", "sync issues"}
        paden = []
        for pad in uit.splitlines():
            pad = pad.strip().strip("/")
            if not pad or pad.lower().startswith(("[gmail]", "[google mail]")):
                continue
            delen = [d.strip().lower() for d in pad.split("/")]
            if len(delen) == 1 and delen[0] in weg_bovenaan:
                continue
            if delen[0] in nooit_onder:
                continue
            paden.append(pad)

        def volgorde(p: str):
            eerste = p.split("/")[0].lower()
            return (0 if eerste == inbox else 2 if eerste == archief else 1, p.lower())
        return sorted(set(paden), key=volgorde)

    def nieuw_pad(self, naam: str) -> str:
        """Waar een nieuwe map komt: in je Inbox (Exchange), of bovenaan (Gmail-labels)."""
        naam = naam.strip().strip("/")
        if self.mappen["archief"].startswith("[Gmail]") or naam.lower().startswith(self.mailbox.lower() + "/"):
            return naam
        return f"{self.mailbox}/{naam}"

    def maak_map(self, pad: str) -> None:
        _osascript(SCRIPT_MAP_MAKEN, self.account, pad)

    def verplaats(self, bron_ids: list[str], doel: str) -> dict[str, str | None]:
        """Verplaatst berichten naar archief, prullenbak of 'map:<pad>'. Niets wordt definitief verwijderd."""
        map_ = doel[4:] if doel.startswith("map:") else self.mappen[doel]
        uit = _osascript(SCRIPT_VERPLAATS, self.account, self.mailbox, map_, *bron_ids)
        resultaat: dict[str, str | None] = {i: "geen antwoord van Apple Mail" for i in bron_ids}
        for regel in uit.splitlines():
            mid, _, status = regel.partition(":")
            if mid.strip() in resultaat:
                resultaat[mid.strip()] = None if status.strip() == "ok" else status.strip()
        return resultaat
