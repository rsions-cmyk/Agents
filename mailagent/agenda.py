"""Agenda: vrije momenten zoeken en afspraken maken in de macOS-agenda (EventKit).

Staat je Exchange-agenda in de app Agenda, dan synchroniseert een afspraak die hier wordt
gemaakt naar Outlook. Er wordt alleen een afspraak gemaakt als jij in de app op Inplannen klikt.

De eerste keer vraagt macOS of Terminal (of Python) bij je agenda mag. Dat moet je toestaan:
Systeeminstellingen, Privacy en beveiliging, Agenda's.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional, Protocol


@dataclass
class Werktijd:
    dagen: set[int] = field(default_factory=lambda: {1, 2, 3, 4, 5})   # 1 = maandag
    begin: time = time(9, 0)
    eind: time = time(17, 0)
    dagen_vooruit: int = 10


def lees_werktijd(ruw: dict | None) -> Werktijd:
    ruw = ruw or {}
    w = Werktijd()
    if "werkdagen" in ruw:
        w.dagen = {int(d) for d in ruw["werkdagen"]}
    if "werktijd" in ruw:
        b, e = str(ruw["werktijd"]).split("-")
        w.begin, w.eind = time.fromisoformat(b.strip()), time.fromisoformat(e.strip())
    if "dagen_vooruit" in ruw:
        w.dagen_vooruit = int(ruw["dagen_vooruit"])
    return w


DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def nl_moment(t: datetime) -> str:
    return f"{DAGEN[t.weekday()]} {t.day} {MAANDEN[t.month - 1]} {t:%H:%M}"


def _volgend_kwartier(t: datetime) -> datetime:
    t = t.replace(second=0, microsecond=0)
    rest = (-t.minute) % 15
    return t + timedelta(minutes=rest)


def vrije_momenten(bezet: list[tuple[datetime, datetime]], minuten: int, nu: datetime, werk: Werktijd,
                   deadline: Optional[date] = None, aantal: int = 3) -> list[datetime]:
    """Geeft tot `aantal` begintijden, hoogstens één per dag, binnen werktijd en vóór de deadline."""
    duur = timedelta(minutes=minuten)
    laatste_dag = (nu + timedelta(days=werk.dagen_vooruit)).date()
    if deadline and deadline < laatste_dag:
        laatste_dag = deadline
    bezet = sorted(bezet)
    gevonden: list[datetime] = []
    dag = nu.date()
    while dag <= laatste_dag and len(gevonden) < aantal:
        if dag.isoweekday() in werk.dagen:
            start = datetime.combine(dag, werk.begin, tzinfo=nu.tzinfo)
            eind_dag = datetime.combine(dag, werk.eind, tzinfo=nu.tzinfo)
            if dag == nu.date():
                start = max(start, _volgend_kwartier(nu + timedelta(minutes=30)))
            t = start
            while t + duur <= eind_dag:
                botsing = next((e for b, e in bezet if b < t + duur and e > t), None)
                if botsing is None:
                    gevonden.append(t)
                    break
                t = _volgend_kwartier(max(botsing, t + timedelta(minutes=15)))
        dag += timedelta(days=1)
    return gevonden


class Agenda(Protocol):
    def bezet(self, van: datetime, tot: datetime) -> list[tuple[datetime, datetime]]: ...
    def maak_afspraak(self, titel: str, start: datetime, eind: datetime, notities: str) -> str: ...


class AppleAgenda:
    """macOS-agenda via EventKit (pip install pyobjc-framework-EventKit)."""

    def __init__(self, kalender: str, account: Optional[str] = None, bezet_agendas: Optional[list[str]] = None):
        import EventKit  # alleen op macOS beschikbaar
        import Foundation
        self.EK, self.NS = EventKit, Foundation
        self.kalender_naam, self.account = kalender, account
        # "Account/Kalender" of alleen "Kalender"; leeg = alle agenda's tellen mee als bezet
        self.bezet_agendas = [b.strip() for b in (bezet_agendas or []) if b.strip()]
        self.store = EventKit.EKEventStore.alloc().init()
        self._toegang()

    def _toegang(self) -> None:
        EK = self.EK
        status = EK.EKEventStore.authorizationStatusForEntityType_(EK.EKEntityTypeEvent)
        if status == 3:  # volledige toegang
            return
        klaar, uitkomst = threading.Event(), {}

        def terug(toegestaan, fout):
            uitkomst["ok"] = bool(toegestaan)
            klaar.set()

        if hasattr(self.store, "requestFullAccessToEventsWithCompletion_"):
            self.store.requestFullAccessToEventsWithCompletion_(terug)
        else:
            self.store.requestAccessToEntityType_completion_(EK.EKEntityTypeEvent, terug)
        klaar.wait(120)
        if not uitkomst.get("ok"):
            raise RuntimeError("Geen toegang tot je agenda. Sta het toe in Systeeminstellingen, "
                               "Privacy en beveiliging, Agenda's, en start de app opnieuw.")

    def kalenders(self) -> list[tuple[str, str, bool]]:
        uit = []
        for k in self.store.calendarsForEntityType_(self.EK.EKEntityTypeEvent):
            uit.append((str(k.source().title()), str(k.title()), bool(k.allowsContentModifications())))
        return uit

    def _kalender(self):
        kandidaten = [k for k in self.store.calendarsForEntityType_(self.EK.EKEntityTypeEvent)
                      if str(k.title()) == self.kalender_naam
                      and (self.account is None or str(k.source().title()) == self.account)]
        if not kandidaten:
            raise RuntimeError(f"Agenda '{self.kalender_naam}' niet gevonden. Bekijk de namen met: mailagent agendas")
        if len(kandidaten) > 1:
            raise RuntimeError(f"Er zijn meerdere agenda's met de naam '{self.kalender_naam}'. "
                               "Zet in config.yaml ook 'account:' (zie: mailagent agendas).")
        return kandidaten[0]

    def _nsdate(self, t: datetime):
        return self.NS.NSDate.dateWithTimeIntervalSince1970_(t.timestamp())

    def _dt(self, d) -> datetime:
        return datetime.fromtimestamp(d.timeIntervalSince1970()).astimezone()

    def bezet(self, van: datetime, tot: datetime) -> list[tuple[datetime, datetime]]:
        kalenders = None
        if self.bezet_agendas:
            kalenders = [k for k in self.store.calendarsForEntityType_(self.EK.EKEntityTypeEvent)
                         if f"{k.source().title()}/{k.title()}" in self.bezet_agendas or str(k.title()) in self.bezet_agendas]
            if not kalenders:
                raise RuntimeError("Geen van de agenda's bij 'bezet:' in config.yaml gevonden (zie: mailagent agendas)")
        pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(self._nsdate(van), self._nsdate(tot), kalenders)
        uit = []
        for ev in self.store.eventsMatchingPredicate_(pred) or []:
            if ev.isAllDay() or ev.availability() == self.EK.EKEventAvailabilityFree:
                continue
            uit.append((self._dt(ev.startDate()), self._dt(ev.endDate())))
        return uit

    def maak_afspraak(self, titel: str, start: datetime, eind: datetime, notities: str) -> str:
        ev = self.EK.EKEvent.eventWithEventStore_(self.store)
        ev.setTitle_(titel)
        ev.setStartDate_(self._nsdate(start))
        ev.setEndDate_(self._nsdate(eind))
        ev.setNotes_(notities)
        ev.setCalendar_(self._kalender())
        ok, fout = self.store.saveEvent_span_error_(ev, self.EK.EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"Afspraak niet opgeslagen: {fout}")
        return f"ingepland in '{self.kalender_naam}' op {nl_moment(start)}"


def maak_agenda(cfg) -> tuple[Optional[Agenda], Optional[str]]:
    """Geeft (agenda, None) of (None, reden waarom er geen agenda is)."""
    ruw = cfg.agenda or {}
    if not ruw.get("kalender"):
        return None, "Geen agenda ingesteld (agenda: kalender: in config.yaml)"
    try:
        return AppleAgenda(ruw["kalender"], ruw.get("account"), ruw.get("bezet")), None
    except ImportError:
        return None, "Agendakoppeling niet geïnstalleerd: .venv/bin/pip install pyobjc-framework-EventKit"
    except Exception as e:
        return None, str(e)
