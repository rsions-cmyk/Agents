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
    status TEXT NOT NULL,       -- wacht | goedgekeurd | afgewezen | ter_info | genegeerd | handmatig | fout | gearchiveerd | verwijderd
    opmerking TEXT,
    aangemaakt TEXT NOT NULL,
    afgehandeld TEXT
);
CREATE TABLE IF NOT EXISTS taken (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id),
    project TEXT NOT NULL,
    omschrijving TEXT NOT NULL,
    deadline TEXT,
    klaar INTEGER NOT NULL DEFAULT 0,
    aangemaakt TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS log (
    tijd TEXT NOT NULL,
    item_id INTEGER,
    gebeurtenis TEXT NOT NULL
);
"""

PRIO_VOLGORDE = "CASE prioriteit WHEN 'hoog' THEN 0 WHEN 'normaal' THEN 1 ELSE 2 END"


def nu() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, pad: Path | str):
        self.pad = str(pad)
        self.db = sqlite3.connect(self.pad, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- bronnen -------------------------------------------------------------
    def laatst_opgehaald(self, bron: str) -> Optional[datetime]:
        r = self.db.execute("SELECT laatst FROM bron_stand WHERE bron=?", (bron,)).fetchone()
        return datetime.fromisoformat(r["laatst"]) if r else None

    def zet_laatst_opgehaald(self, bron: str, tijd: datetime) -> None:
        self.db.execute("INSERT INTO bron_stand(bron, laatst) VALUES(?, ?) "
                        "ON CONFLICT(bron) DO UPDATE SET laatst=excluded.laatst", (bron, tijd.isoformat()))
        self.db.commit()

    # --- mails ---------------------------------------------------------------
    def is_bekend(self, mail: Email) -> bool:
        return self.db.execute("SELECT 1 FROM mails WHERE sleutel=?", (mail.sleutel,)).fetchone() is not None

    def bewaar_mail(self, mail: Email) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO mails VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mail.sleutel, mail.bron, mail.bron_id, mail.van, mail.aan, mail.onderwerp,
             mail.datum.isoformat(), mail.thread_id, mail.tekst[:20000], nu()),
        )
        self.db.commit()

    def mail(self, sleutel: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM mails WHERE sleutel=?", (sleutel,)).fetchone()

    # --- items ---------------------------------------------------------------
    def nieuw_item(self, mail: Email, s: Sortering, u: Optional[Uitwerking], status: str,
                   opmerking: Optional[str] = None) -> int:
        cur = self.db.execute(
            "INSERT INTO items(sleutel, project, actie, prioriteit, deadline, samenvatting, reden, "
            "concept, taken, agenda, vragen, status, opmerking, aangemaakt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mail.sleutel, s.project, s.actie, s.prioriteit, s.deadline, s.samenvatting, s.reden,
             u.concept_antwoord if u else None,
             json.dumps([t.model_dump() for t in u.taken], ensure_ascii=False) if u else "[]",
             u.agenda_voorstel if u else None,
             u.vragen_aan_jou if u else None,
             status, opmerking, nu()),
        )
        self.db.commit()
        self.log(cur.lastrowid, f"aangemaakt ({status})")
        return cur.lastrowid

    def item(self, item_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT i.*, m.van, m.aan, m.onderwerp, m.datum, m.tekst, m.bron, m.bron_id, m.thread_id "
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
    def voeg_taken_toe(self, item_id: int, project: str, taken: list[dict]) -> None:
        for t in taken:
            self.db.execute("INSERT INTO taken(item_id, project, omschrijving, deadline, aangemaakt) VALUES (?,?,?,?,?)",
                            (item_id, project, t["omschrijving"], t.get("deadline"), nu()))
        self.db.commit()

    def open_taken(self, project: Optional[str] = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM taken WHERE klaar=0"
        args: list = []
        if project:
            q += " AND project=?"
            args.append(project)
        q += " ORDER BY deadline IS NULL, deadline"
        return self.db.execute(q, args).fetchall()

    def taak_klaar(self, taak_id: int) -> None:
        self.db.execute("UPDATE taken SET klaar=1 WHERE id=?", (taak_id,))
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
