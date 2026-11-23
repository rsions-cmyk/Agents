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
        self.db.execute("PRAGMA journal_mode=WAL")  # lezen en schrijven tegelijk vanuit meerdere verbindingen
        self.db.executescript(SCHEMA)
        # oudere databases bijwerken
        kolommen = {r["name"] for r in self.db.execute("PRAGMA table_info(taken)")}
        for kolom, soort in (("minuten", "INTEGER"), ("stappen", "TEXT"), ("gepland", "TEXT"), ("afgerond", "TEXT"),
                             ("oordeel", "INTEGER"), ("later", "INTEGER DEFAULT 0")):
            if kolom not in kolommen:
                self.db.execute(f"ALTER TABLE taken ADD COLUMN {kolom} {soort}")
        itemkolommen = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        if "eigen_antwoord" not in itemkolommen:
            self.db.execute("ALTER TABLE items ADD COLUMN eigen_antwoord TEXT")
        if "vorige_status" not in itemkolommen:
            self.db.execute("ALTER TABLE items ADD COLUMN vorige_status TEXT")
        # mail met een open taak hoort niet meer in Beoordelen (oudere versies lieten hem staan)
        self.db.execute("UPDATE items SET status='taak' WHERE status IN ('wacht','fout','handmatig') "
                        "AND id IN (SELECT item_id FROM taken WHERE klaar=0 AND item_id IS NOT NULL)")
        # één item per mail; een oudere database met dubbele items houdt het zonder (de vergrendeling
        # van de ronde voorkomt dan nieuwe dubbele)
        try:
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS items_sleutel_uniek ON items(sleutel)")
        except sqlite3.IntegrityError:
            pass
        self.db.commit()

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
            "INSERT OR IGNORE INTO mails VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mail.sleutel, mail.bron, mail.bron_id, mail.van, mail.aan, mail.onderwerp,
             mail.datum.isoformat(), mail.thread_id, mail.tekst[:20000], nu()),
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
                    "concept, taken, agenda, vragen, status, opmerking, aangemaakt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mail.sleutel, s.project, s.actie, s.prioriteit, s.deadline, s.samenvatting, s.reden,
                     u.concept_antwoord if u else None,
                     json.dumps([t.model_dump() for t in u.taken], ensure_ascii=False) if u else "[]",
                     u.agenda_voorstel if u else None,
                     u.vragen_aan_jou if u else None,
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
        self.db.execute("INSERT OR IGNORE INTO mails SELECT ?, bron, ?, van, aan, onderwerp, datum, thread_id, tekst, opgehaald "
                        "FROM mails WHERE sleutel=?", (f"{bron}:{nieuw_id}", nieuw_id, sleutel))
        self.db.commit()

    def taak(self, taak_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM taken WHERE id=?", (taak_id,)).fetchone()

    def open_taken_bij_mail(self, item_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM taken WHERE item_id=? AND klaar=0", (item_id,)).fetchone()[0]

    def afgeronde_taken(self, limiet: int = 30) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM taken WHERE klaar=1 ORDER BY afgerond IS NULL, afgerond DESC, id DESC LIMIT ?",
                               (limiet,)).fetchall()

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

    def zet_concept(self, item_id: int, concept: str) -> None:
        self.db.execute("UPDATE items SET concept=? WHERE id=?", (concept, item_id))
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
