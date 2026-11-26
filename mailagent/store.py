"""Lokale opslag in SQLite: mails, de goedkeuringswachtrij, taken en de stand per bron."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Email, Sortering, Uitwerking

SCHEMA = """
CREATE TABLE IF NOT EXISTS mails (
    sleutel TEXT PRIMARY KEY,
    bron TEXT NOT NULL,
    bron_id TEXT NOT NULL,
    van TEXT, aan TEXT, onderwerp TEXT,
    datum TEXT, thread_id TEXT, tekst TEXT,
    opgehaald TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sleutel TEXT NOT NULL REFERENCES mails(sleutel),
    project TEXT NOT NULL,
    actie TEXT NOT NULL,
    prioriteit TEXT NOT NULL,
    deadline TEXT,
    samenvatting TEXT,
    reden TEXT,
    concept TEXT,
    taken TEXT,
    agenda TEXT,
    vragen TEXT,
    status TEXT NOT NULL,       -- wacht | goedgekeurd | afgewezen | ter_info | genegeerd | handmatig | fout | gearchiveerd | verwijderd | taak | opgeborgen | gedaan | verzonden
    opmerking TEXT,
    aangemaakt TEXT NOT NULL,
    afgehandeld TEXT,
    eigen_antwoord TEXT          -- de tekst die de eigenaar uiteindelijk goedkeurde of in Mail opende
);
CREATE TABLE IF NOT EXISTS taken (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id),
    project TEXT NOT NULL,
    omschrijving TEXT NOT NULL,
    deadline TEXT,
    klaar INTEGER NOT NULL DEFAULT 0,
    aangemaakt TEXT NOT NULL,
    minuten INTEGER,
    stappen TEXT,
    gepland TEXT,
    afgerond TEXT,
    oordeel INTEGER,             -- -1 korter dan geschat, 0 klopte, 1 langer
    later INTEGER DEFAULT 0      -- bewust geparkeerd bij het afsluiten van de dag
);
CREATE TABLE IF NOT EXISTS bron_stand (
    bron TEXT PRIMARY KEY,
    laatst TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS afzender_regels (
    adres TEXT PRIMARY KEY,      -- e-mailadres in kleine letters
    actie TEXT NOT NULL,         -- 'negeren': voortaan overslaan zonder Claude
    project TEXT,
    aangemaakt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS map_keuzes (
    bron TEXT NOT NULL,
    map TEXT NOT NULL,
    adres TEXT NOT NULL,
    project TEXT,
    aantal INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bron, map, adres, project)
);
CREATE TABLE IF NOT EXISTS belangrijk (
    adres TEXT PRIMARY KEY,      -- e-mailadres of domein (@istte.org), kleine letters
    naam TEXT,
    aangemaakt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wacht_op (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bron TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,   -- van de mail die je verstuurde
    aan TEXT, onderwerp TEXT,
    verstuurd TEXT NOT NULL,
    wat TEXT,                          -- waarop je wacht, in een paar woorden
    herinner_op TEXT NOT NULL,         -- datum waarop hij terugkomt in Te doen
    status TEXT NOT NULL,              -- wacht | beantwoord | klaar | geen
    antwoord TEXT,                     -- sleutel van het antwoord dat binnenkwam
    aangemaakt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS afbeeldingen_ok (
    adres TEXT PRIMARY KEY,      -- afzender van wie externe afbeeldingen altijd mogen laden
    aangemaakt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verstuurd (
    message_id TEXT PRIMARY KEY,     -- mail die je zelf verstuurde (van elk apparaat), als achtergrond
    bron TEXT NOT NULL,
    aan TEXT, onderwerp TEXT, datum TEXT,
    tekst TEXT,                      -- alleen wat je zelf schreef, zonder citaat
    verwijzingen TEXT                -- In-Reply-To en References
);
CREATE TABLE IF NOT EXISTS les_regels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    soort TEXT NOT NULL,         -- titel | organisator | kalender
    waarde TEXT NOT NULL,        -- kleine letters
    les INTEGER NOT NULL,        -- 1: dit is een les, 0: dit is geen les
    aangemaakt TEXT NOT NULL,
    UNIQUE (soort, waarde)
);
CREATE TABLE IF NOT EXISTS afspraak_soorten (
    sleutel TEXT PRIMARY KEY,    -- EventKit-id (één per reeks) of titel en organisator
    soort TEXT NOT NULL,         -- les | afdeling | overleg | eigen
    reden TEXT,
    door TEXT NOT NULL,          -- claude | jij
    titel TEXT,
    aangemaakt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voorlopig (
    afspraak_id TEXT PRIMARY KEY,   -- voorlopig vastgehouden moment in je agenda
    item_id INTEGER NOT NULL,       -- de mail met het verzoek
    titel TEXT, start TEXT, eind TEXT,
    aangemaakt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gemeld (
    sleutel TEXT PRIMARY KEY,
    tijd TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log (
    tijd TEXT NOT NULL,
    item_id INTEGER,
    gebeurtenis TEXT NOT NULL
);
"""

PRIO_VOLGORDE = "CASE prioriteit WHEN 'hoog' THEN 0 WHEN 'normaal' THEN 1 ELSE 2 END"


OPEN_STATUSSEN = ("wacht", "fout", "handmatig", "ter_info", "genegeerd")   # in To do of Tidy up


def normaal_message_id(mid: Optional[str]) -> str:
    """Message-ID zonder punthaken en hoofdletters: Apple Mail en de mail zelf schrijven het niet altijd gelijk."""
    return (mid or "").strip().strip("<>").strip().lower()


def nu() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, pad: Path | str):
        self.pad = str(pad)
        self.db = sqlite3.connect(self.pad, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")  # lezen en schrijven tegelijk vanuit meerdere verbindingen
        self.db.executescript(SCHEMA)
        # oudere databases bijwerken
        kolommen = {r["name"] for r in self.db.execute("PRAGMA table_info(taken)")}
        for kolom, soort in (("minuten", "INTEGER"), ("stappen", "TEXT"), ("gepland", "TEXT"), ("afgerond", "TEXT"),
                             ("oordeel", "INTEGER"), ("later", "INTEGER DEFAULT 0"),
                             ("soort", "TEXT"),           # 'antwoord' voor de taak 'Beantwoorden: …'
                             ("lijkt_klaar", "TEXT"),     # waarom de app denkt dat de taak al gedaan is
                             ("bewijs", "TEXT"),          # Message-ID van het verstuurde antwoord dat is bekeken
                             ("deadline_in_agenda", "TEXT"),    # datum van de deadline-afspraak in je agenda
                             ("afspraak_id", "TEXT")):          # de afspraak in je agenda (EventKit-id)
            if kolom not in kolommen:
                self.db.execute(f"ALTER TABLE taken ADD COLUMN {kolom} {soort}")
        self.db.execute("UPDATE taken SET soort='antwoord' WHERE soort IS NULL "
                        "AND (omschrijving LIKE 'Beantwoorden:%' OR omschrijving LIKE 'Doorsturen:%')")
        itemkolommen = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        if "eigen_antwoord" not in itemkolommen:
            self.db.execute("ALTER TABLE items ADD COLUMN eigen_antwoord TEXT")
        if "vorige_status" not in itemkolommen:
            self.db.execute("ALTER TABLE items ADD COLUMN vorige_status TEXT")
        for kolom in ("lijkt_beantwoord", "bewijs", "deadline_in_agenda", "context",   # context: gebruikte achtergrond
                      "afspraak",                                                      # afspraak: verzoek uit de mail (JSON)
                      "opgeruimd",      # na je antwoord gearchiveerd (tijdstip), of 'weg' als hij al niet meer in de inbox stond
                      "open_punten",    # wat je in het concept nog moet beslissen of aanvullen (JSON)
                      "terug_in_inbox"):  # wanneer je deze mail zelf terugzette in je inbox
            if kolom not in itemkolommen:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {kolom} TEXT")
        wachtkolommen = {r["name"] for r in self.db.execute("PRAGMA table_info(wacht_op)")}
        for kolom in ("tekst", "concept"):   # wat je schreef, en een herinnering die Claude voorstelde
            if kolom not in wachtkolommen:
                self.db.execute(f"ALTER TABLE wacht_op ADD COLUMN {kolom} TEXT")
        if "verwijzingen" not in {r["name"] for r in self.db.execute("PRAGMA table_info(mails)")}:
            self.db.execute("ALTER TABLE mails ADD COLUMN verwijzingen TEXT")   # In-Reply-To en References
        # mail met een open taak hoort niet meer in Beoordelen (oudere versies lieten hem staan)
        self.db.execute("UPDATE items SET status='taak' WHERE status IN ('wacht','fout','handmatig') "
                        "AND id IN (SELECT item_id FROM taken WHERE klaar=0 AND item_id IS NOT NULL)")
        self.voeg_dubbele_samen()
        # één item per mail; een oudere database met dubbele items houdt het zonder (de vergrendeling
        # van de ronde voorkomt dan nieuwe dubbele)
        try:
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS items_sleutel_uniek ON items(sleutel)")
        except sqlite3.IntegrityError:
            pass
        self.db.commit()

    # --- herkennen op Message-ID (fout 5) ----------------------------------------
    def item_bij_message_id(self, bron: str, message_id: str) -> Optional[sqlite3.Row]:
        """Het (oudste) item van deze mail, gevonden op het Message-ID, dat nooit verandert."""
        mid = normaal_message_id(message_id)
        if not mid:
            return None
        r = self.db.execute("SELECT i.id FROM items i JOIN mails m USING(sleutel) WHERE m.bron=? "
                            "AND lower(trim(m.thread_id, '<> ')) = ? ORDER BY i.id LIMIT 1", (bron, mid)).fetchone()
        return self.item(r["id"]) if r else None

    def voeg_dubbele_samen(self) -> int:
        """Eenmalig, voor mail die attaché vóór deze versie dubbel verwerkte: per Message-ID blijft het oudste item
        over. Taken en voorlopige afspraken gaan mee. Stond het nieuwe item nog open (To do of Tidy up), dan neemt
        het oude die stand en het nieuwe concept over, want daar staat de mail nu."""
        rijen = self.db.execute(
            "SELECT i.id, i.status, i.sleutel, m.bron, m.bron_id, lower(trim(m.thread_id, '<> ')) AS mid "
            "FROM items i JOIN mails m USING(sleutel) WHERE trim(COALESCE(m.thread_id, ''), '<> ') <> '' "
            "ORDER BY i.id").fetchall()
        groepen: dict = {}
        for r in rijen:
            groepen.setdefault((r["bron"], r["mid"]), []).append(r)
        samengevoegd = 0
        for groep in groepen.values():
            if len(groep) < 2:
                continue
            houd = groep[0]
            for ander in groep[1:]:
                self.db.execute("UPDATE taken SET item_id=? WHERE item_id=?", (houd["id"], ander["id"]))
                self.db.execute("UPDATE voorlopig SET item_id=? WHERE item_id=?", (houd["id"], ander["id"]))
                if ander["status"] in OPEN_STATUSSEN and houd["status"] not in OPEN_STATUSSEN:
                    self.db.execute(
                        "UPDATE items SET (status, concept, taken, vragen, open_punten, context) = "
                        "(SELECT status, concept, taken, vragen, open_punten, context FROM items WHERE id=?), "
                        "opgeruimd=NULL, terug_in_inbox=? WHERE id=?", (ander["id"], nu(), houd["id"]))
                # het oude item wijst voortaan naar waar de mail nu staat; het nummer van het nieuwe blijft bekend
                self.db.execute("UPDATE mails SET bron_id=? WHERE sleutel=?", (ander["bron_id"], houd["sleutel"]))
                self.db.execute("DELETE FROM items WHERE id=?", (ander["id"],))
                self.log(houd["id"], f"dubbel item {ander['id']} samengevoegd (zelfde Message-ID)")
                samengevoegd += 1
        if samengevoegd:
            self.db.commit()
        return samengevoegd

    def zet_terug_in_inbox(self, item_id: int, status: str, wis_concept: bool) -> None:
        self.db.execute("UPDATE items SET status=?, opgeruimd=NULL, lijkt_beantwoord=NULL, terug_in_inbox=?, "
                        "afgehandeld=? WHERE id=?", (status, nu(), nu(), item_id))
        if wis_concept:
            self.db.execute("UPDATE items SET concept='', open_punten=NULL WHERE id=?", (item_id,))
        self.db.commit()
        self.log(item_id, f"zelf teruggezet in de inbox -> {status}")

    # --- bronnen -------------------------------------------------------------
    def laatst_opgehaald(self, bron: str) -> Optional[datetime]:
        r = self.db.execute("SELECT laatst FROM bron_stand WHERE bron=?", (bron,)).fetchone()
        return datetime.fromisoformat(r["laatst"]) if r else None

    def zet_laatst_opgehaald(self, bron: str, tijd: datetime) -> None:
        self.db.execute("INSERT INTO bron_stand(bron, laatst) VALUES(?, ?) "
                        "ON CONFLICT(bron) DO UPDATE SET laatst=excluded.laatst", (bron, tijd.isoformat()))
        self.db.commit()

    # --- mails ---------------------------------------------------------------
    def is_bekend_sleutel(self, sleutel: str) -> bool:
        return self.db.execute("SELECT 1 FROM mails WHERE sleutel=?", (sleutel,)).fetchone() is not None

    def is_bekend(self, mail: Email) -> bool:
        return self.db.execute("SELECT 1 FROM mails WHERE sleutel=?", (mail.sleutel,)).fetchone() is not None

    def _bewaar_mail(self, mail: Email) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO mails(sleutel, bron, bron_id, van, aan, onderwerp, datum, thread_id, tekst, "
            "opgehaald, verwijzingen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mail.sleutel, mail.bron, mail.bron_id, mail.van, mail.aan, mail.onderwerp,
             mail.datum.isoformat(), mail.thread_id, mail.tekst[:20000], nu(), mail.extra.get("verwijzingen")),
        )

    def bewaar_mail(self, mail: Email) -> None:
        self._bewaar_mail(mail)
        self.db.commit()

    def mail(self, sleutel: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM mails WHERE sleutel=?", (sleutel,)).fetchone()

    # --- items ---------------------------------------------------------------
    def nieuw_item(self, mail: Email, s: Sortering, u: Optional[Uitwerking], status: str,
                   opmerking: Optional[str] = None) -> int:
        """Bewaart de mail en zijn item samen: lukt het item niet, dan geldt de mail ook niet als verwerkt
        en probeert de volgende ronde hem opnieuw."""
        try:
            with self.db:
                self._bewaar_mail(mail)
                cur = self.db.execute(
                    "INSERT INTO items(sleutel, project, actie, prioriteit, deadline, samenvatting, reden, "
                    "concept, taken, agenda, vragen, open_punten, status, opmerking, aangemaakt) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mail.sleutel, s.project, s.actie, s.prioriteit, s.deadline, s.samenvatting, s.reden,
                     u.concept_antwoord if u else None,
                     json.dumps([t.model_dump() for t in u.taken], ensure_ascii=False) if u else "[]",
                     u.agenda_voorstel if u else None,
                     u.vragen_aan_jou if u else None,
                     json.dumps([p.model_dump() for p in u.open_punten], ensure_ascii=False) if u and u.open_punten else None,
                     status, opmerking, nu()),
                )
        except sqlite3.IntegrityError:
            bestaand = self.db.execute("SELECT id FROM items WHERE sleutel=? ORDER BY id LIMIT 1",
                                       (mail.sleutel,)).fetchone()
            if bestaand is None:
                raise
            return bestaand["id"]   # deze mail had al een item
        self.log(cur.lastrowid, f"aangemaakt ({status})")
        return cur.lastrowid

    def item(self, item_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT i.*, m.van, m.aan, m.onderwerp, m.datum, m.tekst, m.bron, m.bron_id, m.thread_id, m.verwijzingen "
            "FROM items i JOIN mails m USING(sleutel) WHERE i.id=?", (item_id,)).fetchone()

    def items(self, status: Optional[str] = None, project: Optional[str] = None,
              sinds: Optional[str] = None) -> list[sqlite3.Row]:
        q = ("SELECT i.*, m.van, m.onderwerp, m.datum FROM items i JOIN mails m USING(sleutel) WHERE 1=1")
        args: list = []
        if status:
            q += " AND i.status=?"
            args.append(status)
        if project:
            q += " AND i.project=?"
            args.append(project)
        if sinds:
            q += " AND i.aangemaakt>=?"
            args.append(sinds)
        q += f" ORDER BY {PRIO_VOLGORDE}, m.datum DESC"
        return self.db.execute(q, args).fetchall()

    def zet_status(self, item_id: int, status: str, concept: Optional[str] = None,
                   opmerking: Optional[str] = None) -> None:
        if concept is not None:
            self.db.execute("UPDATE items SET concept=? WHERE id=?", (concept, item_id))
        self.db.execute("UPDATE items SET status=?, opmerking=COALESCE(?, opmerking), afgehandeld=? WHERE id=?",
                        (status, opmerking, nu(), item_id))
        self.db.commit()
        self.log(item_id, f"status -> {status}" + (f": {opmerking}" if opmerking else ""))

    # --- taken ---------------------------------------------------------------
    def voeg_taken_toe(self, item_id: int, project: str, taken: list[dict]) -> list[int]:
        ids = []
        for t in taken:
            cur = self.db.execute(
                "INSERT INTO taken(item_id, project, omschrijving, deadline, aangemaakt, minuten, soort) VALUES (?,?,?,?,?,?,?)",
                (item_id, project, t["omschrijving"], t.get("deadline"), nu(), t.get("minuten"),
                 "antwoord" if t.get("antwoord") else None))
            ids.append(cur.lastrowid)
        self.db.commit()
        return ids

    def zet_lijkt_klaar(self, taak_id: int, reden: str | None, bewijs: str) -> None:
        """Markering 'lijkt gedaan' (reden) of alleen: dit antwoord is bekeken (reden None)."""
        self.db.execute("UPDATE taken SET lijkt_klaar=?, bewijs=? WHERE id=?", (reden, bewijs, taak_id))
        self.db.commit()

    def wis_lijkt_klaar(self, taak_id: int) -> None:
        """'Nog niet gedaan': markering weg; hetzelfde antwoord markeert de taak niet opnieuw."""
        self.db.execute("UPDATE taken SET lijkt_klaar=NULL WHERE id=?", (taak_id,))
        self.db.commit()

    # --- al beantwoord, belangrijke afzenders, wacht op antwoord, meldingen ---
    def zet_afspraak_id(self, taak_id: int, afspraak_id: str | None) -> None:
        self.db.execute("UPDATE taken SET afspraak_id=? WHERE id=?", (afspraak_id, taak_id))
        self.db.commit()

    def ontplan_taak(self, taak_id: int) -> None:
        self.db.execute("UPDATE taken SET gepland=NULL, afspraak_id=NULL WHERE id=?", (taak_id,))
        self.db.commit()
        self.log(None, f"taak {taak_id} uit de agenda gehaald")

    def zet_deadline_in_agenda(self, soort: str, rij_id: int, dag: str) -> None:
        tabel = "taken" if soort == "taak" else "items"
        self.db.execute(f"UPDATE {tabel} SET deadline_in_agenda=? WHERE id=?", (dag, rij_id))
        self.db.commit()

    def zet_lijkt_beantwoord(self, item_id: int, reden: str | None, bewijs: str) -> None:
        self.db.execute("UPDATE items SET lijkt_beantwoord=?, bewijs=? WHERE id=?", (reden, bewijs, item_id))
        self.db.commit()

    def op_te_ruimen(self, dagen: int = 14) -> list[sqlite3.Row]:
        """Mail die je via de app beantwoordde (Reply in Mail of Send) en die nog in je inbox staat."""
        from datetime import timedelta
        sinds = (datetime.now().astimezone() - timedelta(days=dagen)).isoformat(timespec="seconds")
        return self.db.execute(
            "SELECT i.id, i.status, i.afgehandeld, m.bron, m.bron_id, m.thread_id FROM items i JOIN mails m USING(sleutel) "
            "WHERE i.status IN ('goedgekeurd','verzonden') AND i.opgeruimd IS NULL AND i.eigen_antwoord IS NOT NULL "
            "AND i.afgehandeld >= ? ORDER BY i.afgehandeld", (sinds,)).fetchall()

    def zet_opgeruimd(self, item_id: int, waarde: str | None) -> None:
        self.db.execute("UPDATE items SET opgeruimd=? WHERE id=?", (waarde, item_id))
        self.db.commit()

    def wis_lijkt_beantwoord(self, item_id: int) -> None:
        self.db.execute("UPDATE items SET lijkt_beantwoord=NULL WHERE id=?", (item_id,))
        self.db.commit()

    def belangrijke(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM belangrijk ORDER BY adres").fetchall()

    def zet_belangrijk(self, adres: str, naam: str | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO belangrijk(adres, naam, aangemaakt) VALUES (?,?,?)",
                        (adres.strip().lower(), naam, nu()))
        self.db.commit()

    def verwijder_belangrijk(self, adres: str) -> None:
        self.db.execute("DELETE FROM belangrijk WHERE adres=?", (adres.strip().lower(),))
        self.db.commit()

    def wacht_op_bekend(self, message_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM wacht_op WHERE message_id=?", (message_id,)).fetchone() is not None

    def nieuw_wacht_op(self, bron: str, message_id: str, aan: str, onderwerp: str, verstuurd: str,
                       wat: str | None, herinner_op: str, status: str = "wacht", tekst: str | None = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO wacht_op(bron, message_id, aan, onderwerp, verstuurd, wat, herinner_op, "
                        "status, aangemaakt, tekst) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (bron, message_id, aan, onderwerp, verstuurd, wat, herinner_op, status, nu(),
                         tekst if status == "wacht" else None))
        self.db.commit()

    def vul_wacht_tekst(self, message_id: str, tekst: str) -> None:
        """Tekst aanvullen voor wat al op de lijst stond voordat de app die bewaarde."""
        self.db.execute("UPDATE wacht_op SET tekst=? WHERE message_id=? AND status='wacht' AND (tekst IS NULL OR tekst='')",
                        (tekst, message_id))
        self.db.commit()

    def zet_wacht_concept(self, wacht_id: int, concept: str) -> None:
        self.db.execute("UPDATE wacht_op SET concept=? WHERE id=?", (concept, wacht_id))
        self.db.commit()

    def wacht_op_lijst(self, status: str = "wacht") -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wacht_op WHERE status=? ORDER BY herinner_op, verstuurd",
                               (status,)).fetchall()

    def wacht_op(self, wacht_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wacht_op WHERE id=?", (wacht_id,)).fetchone()

    def zet_wacht_op(self, wacht_id: int, status: str | None = None, herinner_op: str | None = None,
                     antwoord: str | None = None) -> None:
        if status:
            self.db.execute("UPDATE wacht_op SET status=? WHERE id=?", (status, wacht_id))
        if herinner_op:
            self.db.execute("UPDATE wacht_op SET herinner_op=? WHERE id=?", (herinner_op, wacht_id))
        if antwoord:
            self.db.execute("UPDATE wacht_op SET antwoord=? WHERE id=?", (antwoord, wacht_id))
        self.db.commit()

    def inkomend_sinds(self, sinds: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT sleutel, van, onderwerp, datum, verwijzingen FROM mails WHERE datum>?",
                               (sinds,)).fetchall()

    def afbeeldingen_ok(self, adres: str) -> bool:
        return self.db.execute("SELECT 1 FROM afbeeldingen_ok WHERE adres=?", (adres.lower(),)).fetchone() is not None

    def zet_afbeeldingen_ok(self, adres: str, aan: bool) -> None:
        if aan:
            self.db.execute("INSERT OR IGNORE INTO afbeeldingen_ok(adres, aangemaakt) VALUES (?,?)", (adres.lower(), nu()))
        else:
            self.db.execute("DELETE FROM afbeeldingen_ok WHERE adres=?", (adres.lower(),))
        self.db.commit()

    def bewaar_verstuurd(self, bron: str, berichten: list[dict]) -> None:
        for v in berichten:
            if v.get("message_id"):
                self.db.execute("INSERT OR REPLACE INTO verstuurd(message_id, bron, aan, onderwerp, datum, tekst, verwijzingen) "
                                "VALUES (?,?,?,?,?,?,?)",
                                (v["message_id"], bron, v.get("aan"), v.get("onderwerp"), v["datum"].isoformat(),
                                 v.get("eigen", v.get("tekst", ""))[:3000], f"{v.get('in_reply_to', '')} {v.get('references', '')}".strip()))
        self.db.commit()

    def verstuurd_bericht(self, message_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM verstuurd WHERE message_id=?", (message_id,)).fetchone()

    def verstuurd_lijst(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM verstuurd").fetchall()

    def mails_met_tekst(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT sleutel, van, aan, onderwerp, datum, tekst, thread_id, verwijzingen FROM mails").fetchall()

    def zet_context(self, item_id: int, context: list[dict]) -> None:
        self.db.execute("UPDATE items SET context=? WHERE id=?", (json.dumps(context, ensure_ascii=False), item_id))
        self.db.commit()

    def afspraak_soorten(self) -> dict[str, dict]:
        return {r["sleutel"]: dict(r) for r in self.db.execute("SELECT * FROM afspraak_soorten")}

    def zet_afspraak_soort(self, sleutel: str, soort: str, reden: str | None, door: str, titel: str | None = None) -> None:
        if door == "claude":   # nooit over jouw eigen keuze heen
            self.db.execute("INSERT INTO afspraak_soorten(sleutel, soort, reden, door, titel, aangemaakt) VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(sleutel) DO UPDATE SET soort=excluded.soort, reden=excluded.reden "
                            "WHERE afspraak_soorten.door='claude'", (sleutel, soort, reden, door, titel, nu()))
        else:
            self.db.execute("INSERT OR REPLACE INTO afspraak_soorten(sleutel, soort, reden, door, titel, aangemaakt) "
                            "VALUES (?,?,?,?,?,?)", (sleutel, soort, reden, door, titel, nu()))
        self.db.commit()

    def wis_afspraak_soort(self, sleutel: str) -> None:
        self.db.execute("DELETE FROM afspraak_soorten WHERE sleutel=?", (sleutel,))
        self.db.commit()

    def zet_afspraak_verzoek(self, item_id: int, verzoek: dict) -> None:
        self.db.execute("UPDATE items SET afspraak=? WHERE id=?", (json.dumps(verzoek, ensure_ascii=False), item_id))
        self.db.commit()

    def bewaar_voorlopig(self, afspraak_id: str, item_id: int, titel: str, start: str, eind: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO voorlopig(afspraak_id, item_id, titel, start, eind, aangemaakt) VALUES (?,?,?,?,?,?)",
                        (afspraak_id, item_id, titel, start, eind, nu()))
        self.db.commit()

    def voorlopige(self, item_id: int | None = None) -> list[sqlite3.Row]:
        if item_id is None:
            return self.db.execute("SELECT * FROM voorlopig ORDER BY start").fetchall()
        return self.db.execute("SELECT * FROM voorlopig WHERE item_id=? ORDER BY start", (item_id,)).fetchall()

    def voorlopig(self, afspraak_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM voorlopig WHERE afspraak_id=?", (afspraak_id,)).fetchone()

    def wis_voorlopig(self, afspraak_id: str) -> None:
        self.db.execute("DELETE FROM voorlopig WHERE afspraak_id=?", (afspraak_id,))
        self.db.commit()

    TE_DOEN = ("wacht", "fout", "handmatig")

    def afgehandeld_uit_te_doen(self, dagen: int = 21, maximum: int = 150) -> list[sqlite3.Row]:
        """Mail die in Te doen stond en die je afhandelde: gedaan, in Mail gezet, verstuurd, afgewezen, taak gemaakt,
        of gearchiveerd, opgeborgen of weggegooid. Nieuwste eerst."""
        from datetime import timedelta
        sinds = (datetime.now().astimezone() - timedelta(days=dagen)).isoformat(timespec="seconds")
        return self.db.execute(
            "SELECT i.*, m.van, m.onderwerp, m.datum FROM items i JOIN mails m USING(sleutel) "
            "WHERE i.afgehandeld >= ? AND (i.status IN ('gedaan','goedgekeurd','verzonden','afgewezen','taak') "
            "OR (i.status IN ('gearchiveerd','opgeborgen','verwijderd') AND i.vorige_status IN ('wacht','fout','handmatig'))) "
            "ORDER BY i.afgehandeld DESC LIMIT ?", (sinds, maximum)).fetchall()

    def zoek(self, vraag: str, maximum: int = 40) -> dict:
        """Zoeken in alle verwerkte mail (afzender, onderwerp, tekst, samenvatting, je antwoord), taken en
        'wacht op antwoord'. Alle woorden moeten voorkomen."""
        woorden = [w for w in vraag.lower().split() if w][:6]
        if not woorden:
            return {"mail": [], "taken": [], "wacht": []}
        velden = "lower(coalesce(m.van,'') || ' ' || coalesce(m.onderwerp,'') || ' ' || coalesce(i.samenvatting,'') || ' ' || " \
                 "coalesce(i.eigen_antwoord,'') || ' ' || coalesce(m.tekst,''))"
        waar = " AND ".join([f"instr({velden}, ?) > 0"] * len(woorden))
        mail = self.db.execute(f"SELECT i.*, m.van, m.onderwerp, m.datum FROM items i JOIN mails m USING(sleutel) WHERE {waar} "
                               "ORDER BY m.datum DESC LIMIT ?", (*woorden, maximum)).fetchall()
        tw = " AND ".join(["instr(lower(omschrijving || ' ' || coalesce(stappen,'')), ?) > 0"] * len(woorden))
        taken = self.db.execute(f"SELECT * FROM taken WHERE {tw} ORDER BY klaar, coalesce(gepland, aangemaakt) DESC LIMIT 20",
                                woorden).fetchall()
        ww = " AND ".join(["instr(lower(coalesce(onderwerp,'') || ' ' || coalesce(aan,'') || ' ' || coalesce(tekst,'')), ?) > 0"] * len(woorden))
        wacht = self.db.execute(f"SELECT * FROM wacht_op WHERE status='wacht' AND {ww} ORDER BY verstuurd DESC LIMIT 10",
                                woorden).fetchall()
        return {"mail": mail, "taken": taken, "wacht": wacht}

    def les_regels(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM les_regels ORDER BY id").fetchall()

    def zet_les_regel(self, soort: str, waarde: str, les: bool) -> int:
        self.db.execute("INSERT INTO les_regels(soort, waarde, les, aangemaakt) VALUES (?,?,?,?) "
                        "ON CONFLICT(soort, waarde) DO UPDATE SET les=excluded.les",
                        (soort, waarde.strip().lower(), 1 if les else 0, nu()))
        self.db.commit()
        return self.db.execute("SELECT id FROM les_regels WHERE soort=? AND waarde=?", (soort, waarde.strip().lower())).fetchone()["id"]

    def verwijder_les_regel(self, regel_id: int) -> None:
        self.db.execute("DELETE FROM les_regels WHERE id=?", (regel_id,))
        self.db.commit()

    def is_gemeld(self, sleutel: str) -> bool:
        return self.db.execute("SELECT 1 FROM gemeld WHERE sleutel=?", (sleutel,)).fetchone() is not None

    def zet_gemeld(self, sleutel: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO gemeld(sleutel, tijd) VALUES (?,?)", (sleutel, nu()))
        self.db.commit()

    def verwijder_open_taken(self, taak_ids: list[int]) -> int:
        """Alleen taken die nog open en niet ingepland zijn (voor ongedaan maken)."""
        n = 0
        for i in taak_ids:
            n += self.db.execute("DELETE FROM taken WHERE id=? AND klaar=0 AND gepland IS NULL", (i,)).rowcount
        self.db.commit()
        return n

    def nieuwe_taak(self, item_id: Optional[int], project: str, titel: str, deadline: Optional[str],
                    minuten: Optional[int], stappen: list[str], gepland: Optional[str]) -> int:
        cur = self.db.execute(
            "INSERT INTO taken(item_id, project, omschrijving, deadline, aangemaakt, minuten, stappen, gepland) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (item_id, project, titel, deadline, nu(), minuten, json.dumps(stappen, ensure_ascii=False), gepland))
        self.db.commit()
        self.log(item_id, f"taak gemaakt: {titel}" + (f" (gepland {gepland})" if gepland else ""))
        return cur.lastrowid

    def open_taken(self, project: Optional[str] = None) -> list[sqlite3.Row]:  # noqa: D401
        q = "SELECT * FROM taken WHERE klaar=0"
        args: list = []
        if project:
            q += " AND project=?"
            args.append(project)
        q += " ORDER BY deadline IS NULL, deadline"
        return self.db.execute(q, args).fetchall()

    def taak_klaar(self, taak_id: int) -> None:
        self.db.execute("UPDATE taken SET klaar=1, afgerond=? WHERE id=?", (nu(), taak_id))
        self.db.commit()
        self.log(None, f"taak {taak_id} afgevinkt")

    def taak_heropen(self, taak_id: int) -> None:
        self.db.execute("UPDATE taken SET klaar=0, afgerond=NULL WHERE id=?", (taak_id,))
        self.db.commit()
        self.log(None, f"taak {taak_id} teruggezet")

    def plan_taak(self, taak_id: int, gepland: str, minuten: int) -> None:
        self.db.execute("UPDATE taken SET gepland=?, minuten=? WHERE id=?", (gepland, minuten, taak_id))
        self.db.commit()
        self.log(None, f"taak {taak_id} ingepland op {gepland}")

    def zet_project(self, item_id: int, project: str) -> None:
        self.db.execute("UPDATE items SET project=? WHERE id=?", (project, item_id))
        self.db.execute("UPDATE taken SET project=? WHERE item_id=?", (project, item_id))
        self.db.commit()
        self.log(item_id, f"project gecorrigeerd naar {project}")

    def taak_oordeel(self, taak_id: int, oordeel: int) -> None:
        self.db.execute("UPDATE taken SET oordeel=? WHERE id=?", (oordeel, taak_id))
        self.db.commit()

    def taak_later(self, taak_id: int, later: bool) -> None:
        self.db.execute("UPDATE taken SET later=? WHERE id=?", (1 if later else 0, taak_id))
        self.db.commit()

    FACTOR = {-1: 0.75, 0: 1.0, 1: 1.4}

    def tijdfactor(self, project: str, n: int = 10) -> tuple[float, int]:
        """Hoeveel langer of korter taken in dit project duurden dan geschat (laatste n oordelen)."""
        rijen = self.db.execute("SELECT oordeel FROM taken WHERE project=? AND oordeel IS NOT NULL "
                                "ORDER BY afgerond DESC LIMIT ?", (project, n)).fetchall()
        if not rijen:
            return 1.0, 0
        return sum(self.FACTOR[r["oordeel"]] for r in rijen) / len(rijen), len(rijen)

    def vandaag_afgehandeld(self) -> int:
        begin = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        return self.db.execute("SELECT COUNT(*) FROM items WHERE afgehandeld >= ? AND status IN "
                               "('goedgekeurd','afgewezen','gearchiveerd','verwijderd','opgeborgen','taak','gedaan','verzonden')",
                               (begin,)).fetchone()[0]

    def bewaar_vorige_status(self, item_id: int, status: str) -> None:
        self.db.execute("UPDATE items SET vorige_status=? WHERE id=?", (status, item_id))
        self.db.commit()

    def nieuw_bron_id(self, sleutel: str, bron: str, nieuw_id: str) -> None:
        """Na terugzetten geeft Apple Mail de mail een nieuw id: bijwerken, en het nieuwe id als bekend registreren."""
        self.db.execute("UPDATE mails SET bron_id=? WHERE sleutel=?", (nieuw_id, sleutel))
        # kolommen bij naam: de tabel kreeg er later een bij (verwijzingen), en zonder namen faalt dit dan
        self.db.execute("INSERT OR IGNORE INTO mails(sleutel, bron, bron_id, van, aan, onderwerp, datum, thread_id, tekst, "
                        "opgehaald, verwijzingen) SELECT ?, bron, ?, van, aan, onderwerp, datum, thread_id, tekst, opgehaald, "
                        "verwijzingen FROM mails WHERE sleutel=?", (f"{bron}:{nieuw_id}", nieuw_id, sleutel))
        self.db.commit()

    def taak(self, taak_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM taken WHERE id=?", (taak_id,)).fetchone()

    def open_taken_bij_mail(self, item_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM taken WHERE item_id=? AND klaar=0", (item_id,)).fetchone()[0]

    def afgeronde_taken(self, limiet: int = 30) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM taken WHERE klaar=1 ORDER BY afgerond IS NULL, afgerond DESC, id DESC LIMIT ?",
                               (limiet,)).fetchall()

    def vergeet_bron(self, bron: str) -> dict:
        """Alles wat de app van één account bewaarde weghalen: mail, items, wacht op antwoord, verzonden mail,
        mapkeuzes en meldingen. Taken die je van zo'n mail maakte blijven bestaan, los van de mail.
        Raakt Apple Mail en je agenda niet aan."""
        ids = [r[0] for r in self.db.execute(
            "SELECT i.id FROM items i JOIN mails m USING(sleutel) WHERE m.bron=?", (bron,))]
        tel = {"mails": self.db.execute("SELECT COUNT(*) FROM mails WHERE bron=?", (bron,)).fetchone()[0], "items": len(ids)}
        with self.db:
            if ids:
                vraag = ",".join("?" * len(ids))
                tel["taken_losgekoppeld"] = self.db.execute(f"UPDATE taken SET item_id=NULL WHERE item_id IN ({vraag})", ids).rowcount
                self.db.execute(f"DELETE FROM voorlopig WHERE item_id IN ({vraag})", ids)
                self.db.execute(f"DELETE FROM log WHERE item_id IN ({vraag})", ids)
                self.db.execute(f"DELETE FROM items WHERE id IN ({vraag})", ids)
            self.db.execute("DELETE FROM mails WHERE bron=?", (bron,))
            for tabel in ("wacht_op", "verstuurd", "map_keuzes", "bron_stand"):
                self.db.execute(f"DELETE FROM {tabel} WHERE bron=?", (bron,))
            self.db.execute("DELETE FROM gemeld WHERE sleutel LIKE ?", (bron + ":%",))
        return tel

    def zet_eigen_antwoord(self, item_id: int, tekst: str) -> None:
        self.db.execute("UPDATE items SET eigen_antwoord=? WHERE id=?", (tekst, item_id))
        self.db.commit()

    def eigen_antwoorden(self, limiet: int = 200) -> list[sqlite3.Row]:
        """Antwoorden van de eigenaar, nieuwste eerst, met de mail erbij."""
        return self.db.execute(
            "SELECT i.id, i.project, i.eigen_antwoord, m.van, m.onderwerp FROM items i JOIN mails m USING(sleutel) "
            "WHERE i.eigen_antwoord IS NOT NULL AND TRIM(i.eigen_antwoord) != '' "
            "ORDER BY COALESCE(i.afgehandeld, i.aangemaakt) DESC LIMIT ?", (limiet,)).fetchall()

    def afzender_gedrag(self) -> dict[str, dict[str, int]]:
        """Per afzenderadres: hoe vaak gearchiveerd, weggegooid, taak van gemaakt, beantwoord, afgewezen."""
        from email.utils import parseaddr
        soorten = {"gearchiveerd": "archief", "verwijderd": "prullenbak", "taak": "taak",
                   "goedgekeurd": "antwoord", "verzonden": "antwoord", "afgewezen": "afgewezen", "gedaan": "gedaan"}
        uit: dict[str, dict[str, int]] = {}
        for r in self.db.execute("SELECT i.status, m.van FROM items i JOIN mails m USING(sleutel) WHERE i.status IN "
                                 "('gearchiveerd','verwijderd','taak','goedgekeurd','verzonden','afgewezen','gedaan')"):
            adres = (parseaddr(r["van"] or "")[1] or (r["van"] or "")).lower()
            teller = uit.setdefault(adres, {})
            teller[soorten[r["status"]]] = teller.get(soorten[r["status"]], 0) + 1
        return uit

    def onthoud_map(self, bron: str, map_: str, adres: str, project: str | None) -> None:
        self.db.execute("INSERT INTO map_keuzes VALUES (?,?,?,?,1) ON CONFLICT(bron, map, adres, project) "
                        "DO UPDATE SET aantal = aantal + 1", (bron, map_, adres.lower(), project))
        self.db.commit()

    def map_keuzes(self, bron: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM map_keuzes WHERE bron=?", (bron,)).fetchall()

    def zet_concept(self, item_id: int, concept: str, vragen: Optional[str] = None,
                    open_punten: Optional[list] = None) -> None:
        self.db.execute("UPDATE items SET concept=? WHERE id=?", (concept, item_id))
        if vragen is not None:
            self.db.execute("UPDATE items SET vragen=? WHERE id=?", (vragen or None, item_id))
        if open_punten is not None:
            self.db.execute("UPDATE items SET open_punten=? WHERE id=?",
                            (json.dumps(open_punten, ensure_ascii=False) if open_punten else None, item_id))
        self.db.commit()

    # --- geleerde afzenderregels ---------------------------------------------
    def afzender_regel(self, adres: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM afzender_regels WHERE adres=?", (adres.lower(),)).fetchone()

    def zet_afzender_regel(self, adres: str, actie: str, project: Optional[str]) -> None:
        self.db.execute("INSERT INTO afzender_regels VALUES (?,?,?,?) ON CONFLICT(adres) DO UPDATE SET "
                        "actie=excluded.actie, project=excluded.project", (adres.lower(), actie, project, nu()))
        self.db.commit()
        self.log(None, f"afzenderregel {adres.lower()} -> {actie}")

    def verwijder_afzender_regel(self, adres: str) -> None:
        self.db.execute("DELETE FROM afzender_regels WHERE adres=?", (adres.lower(),))
        self.db.commit()
        self.log(None, f"afzenderregel {adres.lower()} verwijderd")

    def afzender_regels(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM afzender_regels ORDER BY adres").fetchall()

    # --- log -----------------------------------------------------------------
    def log(self, item_id: Optional[int], gebeurtenis: str) -> None:
        self.db.execute("INSERT INTO log VALUES (?,?,?)", (nu(), item_id, gebeurtenis))
        self.db.commit()
