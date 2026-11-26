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
    # in de tijdzone van deze Mac rekent elke dag met zijn eigen offset, zodat 09:00 ook na de overgang
    # naar zomer- of wintertijd 09:00 blijft; een andere vaste tijdzone (tests) blijft zoals hij is
    lokaal = nu.utcoffset() == datetime.now().astimezone().utcoffset() or nu.tzinfo is None

    def om(d, t):
        return datetime.combine(d, t).astimezone() if lokaal else datetime.combine(d, t, tzinfo=nu.tzinfo)
    while dag <= laatste_dag and len(gevonden) < aantal:
        if dag.isoweekday() in werk.dagen:
            start = om(dag, werk.begin)
            eind_dag = om(dag, werk.eind)
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


LES_WOORDEN = ["college", "hoorcollege", "werkcollege", "responsiecollege", "lecture", "tutorial", "practicum",
               "les", "lessen", "lesson", "class", "seminar", "colloquium", "tentamen", "exam"]


def soort_afspraak(a: dict, lessen: dict | None = None) -> str:
    """'les', 'overleg' (met anderen) of '' (eigen afspraak). Lessen herken je aan de agenda (bijvoorbeeld een
    roosteragenda), de organisator of woorden in titel of locatie; in te stellen bij agenda: lessen: in config.yaml."""
    import re
    lessen = lessen or {}
    kalenders = [k.lower() for k in lessen.get("kalenders", [])]
    organisatoren = [o.lower() for o in lessen.get("organisatoren", [])]
    woorden = [w.lower() for w in lessen.get("trefwoorden", LES_WOORDEN)]
    org = a.get("organisator") or {}
    tekst = f"{a.get('titel', '')} {a.get('locatie', '')}".lower()
    if (kalenders and str(a.get("kalender", "")).lower() in kalenders) \
            or (organisatoren and any(o in f"{org.get('naam', '')} {org.get('adres', '')}".lower() for o in organisatoren)) \
            or any(re.search(rf"\b{re.escape(w)}\b", tekst) for w in woorden):
        return "les"
    if any(not d.get("ik") for d in a.get("deelnemers") or []):
        return "overleg"
    return ""


class Agenda(Protocol):
    def bezet(self, van: datetime, tot: datetime) -> list[tuple[datetime, datetime]]: ...
    def maak_afspraak(self, titel: str, start: datetime, eind: datetime, notities: str) -> str: ...
    def maak_deadline(self, titel: str, dag: date, notities: str) -> str: ...


class AppleAgenda:
    """macOS-agenda via EventKit (pip install pyobjc-framework-EventKit)."""

    def __init__(self, kalender: str, account: Optional[str] = None, bezet_agendas: Optional[list[str]] = None,
                 herinnering_minuten: Optional[int] = 15, prive: bool = True):
        import EventKit  # alleen op macOS beschikbaar
        import Foundation
        self.EK, self.NS = EventKit, Foundation
        self.kalender_naam, self.account = kalender, account
        self.herinnering_minuten, self.prive = herinnering_minuten, prive
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

    def afspraken(self, van: datetime, tot: datetime) -> list[dict]:
        """Afspraken uit de agenda's die als bezet tellen, met titel, voor het dagoverzicht."""
        kalenders = None
        if self.bezet_agendas:
            kalenders = [k for k in self.store.calendarsForEntityType_(self.EK.EKEntityTypeEvent)
                         if f"{k.source().title()}/{k.title()}" in self.bezet_agendas or str(k.title()) in self.bezet_agendas]
            if not kalenders:
                raise RuntimeError("Geen van de agenda's bij 'bezet:' in config.yaml gevonden (zie: mailagent agendas)")
        pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(self._nsdate(van), self._nsdate(tot), kalenders)
        uit = []
        for ev in self.store.eventsMatchingPredicate_(pred) or []:
            a = {
                "titel": str(ev.title() or "Bezet"),
                "start": self._dt(ev.startDate()), "eind": self._dt(ev.endDate()),
                "heledag": bool(ev.isAllDay()),
                "vrij": ev.availability() == self.EK.EKEventAvailabilityFree,
                "kalender": str(ev.calendar().title()),
            }
            try:   # details voor het venstertje in Planning; blijven op deze Mac
                a["id"] = str(ev.eventIdentifier() or "")
                a["locatie"] = str(ev.location() or "")
                a["notities"] = str(ev.notes() or "")[:4000]
                a["url"] = str(ev.URL().absoluteString()) if ev.URL() else ""
                a["organisator"] = self._deelnemer(ev.organizer()) if ev.organizer() else None
                a["deelnemers"] = [self._deelnemer(d) for d in (ev.attendees() or [])][:40]
                # zelf verschuiven mag alleen bij je eigen afspraken zonder andere deelnemers
                a["wijzigbaar"] = bool(ev.calendar().allowsContentModifications()) and not bool(ev.hasAttendees())
            except Exception:
                pass
            uit.append(a)
        return uit

    STATUS = {0: "", 1: "nog geen antwoord", 2: "ja", 3: "nee", 4: "misschien", 5: "gedelegeerd", 6: "klaar", 7: "bezig"}

    def _deelnemer(self, d) -> dict:
        url = str(d.URL().absoluteString()) if d.URL() else ""
        return {"naam": str(d.name() or ""), "adres": url.replace("mailto:", ""),
                "status": self.STATUS.get(int(d.participantStatus()), ""), "ik": bool(d.isCurrentUser())}

    def zoek_afspraak(self, start: datetime, titel: str) -> Optional[str]:
        """Id van de afspraak op `start` waarvan de titel met `titel` begint (voor taken van vóór deze versie)."""
        pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(
            self._nsdate(start - timedelta(minutes=1)), self._nsdate(start + timedelta(minutes=1)), [self._kalender()])
        for ev in self.store.eventsMatchingPredicate_(pred) or []:
            if str(ev.title() or "").startswith(titel[:40]):
                return str(ev.eventIdentifier())
        return None

    def _exemplaar(self, afspraak_id: str, oud_start: Optional[datetime]):
        """De afspraak zelf; bij een herhaalde afspraak het exemplaar op `oud_start`."""
        if oud_start is not None:
            pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(
                self._nsdate(oud_start - timedelta(minutes=1)), self._nsdate(oud_start + timedelta(minutes=1)), None)
            for ev in self.store.eventsMatchingPredicate_(pred) or []:
                if str(ev.eventIdentifier() or "") == afspraak_id:
                    return ev
        return self.store.eventWithIdentifier_(afspraak_id)

    def verplaats_afspraak(self, afspraak_id: str, oud_start: Optional[datetime], start: datetime, eind: datetime) -> str:
        """Afspraak naar een ander moment (alleen dit exemplaar). Niet bij vergaderingen met anderen."""
        ev = self._exemplaar(afspraak_id, oud_start)
        if ev is None:
            raise RuntimeError("afspraak niet gevonden in je agenda")
        if ev.hasAttendees():
            raise RuntimeError("dit is een vergadering met anderen; verzet die in Outlook, dan krijgen zij de wijziging ook")
        if not ev.calendar().allowsContentModifications():
            raise RuntimeError(f"de agenda '{ev.calendar().title()}' kun je niet wijzigen")
        ev.setStartDate_(self._nsdate(start))
        ev.setEndDate_(self._nsdate(eind))
        ok, fout = self.store.saveEvent_span_error_(ev, self.EK.EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"Afspraak niet verzet: {fout}")
        return f"verzet naar {nl_moment(start)}–{eind:%H:%M}"

    def verwijder_afspraak(self, afspraak_id: str) -> bool:
        ev = self.store.eventWithIdentifier_(afspraak_id)
        if ev is None:
            return False
        ok, fout = self.store.removeEvent_span_error_(ev, self.EK.EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"Afspraak niet verwijderd: {fout}")
        return True

    def bezet(self, van: datetime, tot: datetime) -> list[tuple[datetime, datetime]]:
        return [(a["start"], a["eind"]) for a in self.afspraken(van, tot) if not a["heledag"] and not a["vrij"]]

    def _maak_prive(self, ev, kalender) -> bool:
        """Afspraak privé maken (slotje in Outlook): collega's zien alleen dat je bezet bent.

        EventKit heeft hier geen officiële functie voor. De app Agenda gebruikt een verborgen
        instelling (privacyLevel); het account zelf zegt welke waarde 'privé' is. Lukt het niet,
        dan wordt de afspraak gewoon opgeslagen, zonder slotje."""
        try:
            bron = kalender.source()
            if not (ev.respondsToSelector_("setPrivacyLevel:") and bron.respondsToSelector_("supportsPrivateEvents")
                    and bron.respondsToSelector_("preferredEventPrivateValue")):
                return False
            if not bron.supportsPrivateEvents():
                return False
            if ev.respondsToSelector_("allowsPrivacyLevelModifications") and not ev.allowsPrivacyLevelModifications():
                return False
            waarde = int(bron.preferredEventPrivateValue())
            if not waarde:
                return False
            ev.setPrivacyLevel_(waarde)
            return int(ev.privacyLevel()) == waarde
        except Exception:
            return False

    def _nieuwe_afspraak(self, titel: str, start: datetime, eind: datetime, notities: str):
        ev = self.EK.EKEvent.eventWithEventStore_(self.store)
        ev.setTitle_(titel)
        ev.setStartDate_(self._nsdate(start))
        ev.setEndDate_(self._nsdate(eind))
        ev.setNotes_(notities)
        kalender = self._kalender()
        ev.setCalendar_(kalender)
        if self.herinnering_minuten:
            ev.addAlarm_(self.EK.EKAlarm.alarmWithRelativeOffset_(-60 * int(self.herinnering_minuten)))
        slot = self._maak_prive(ev, kalender) if self.prive else False
        ok, fout = self.store.saveEvent_span_error_(ev, self.EK.EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"Afspraak niet opgeslagen: {fout}")
        return ev, slot

    def maak_afspraak(self, titel: str, start: datetime, eind: datetime, notities: str) -> str:
        ev, slot = self._nieuwe_afspraak(titel, start, eind, notities)
        self.laatste_id = str(ev.eventIdentifier() or "")   # om de taak later te kunnen verzetten of weghalen
        extra = []
        if self.herinnering_minuten:
            extra.append(f"herinnering {self.herinnering_minuten} min vooraf")
        if self.prive:
            extra.append("privé" if slot else "niet privé: deze agenda laat dat niet toe")
        return f"ingepland in '{self.kalender_naam}' op {nl_moment(start)}" + (f" ({', '.join(extra)})" if extra else "")

    def maak_deadline(self, titel: str, dag: date, notities: str) -> str:
        """Deadline als hele-dag-afspraak: telt niet als bezet, met slotje, herinnering de dag ervoor om 09:00."""
        ev = self.EK.EKEvent.eventWithEventStore_(self.store)
        ev.setTitle_(titel)
        begin = datetime.combine(dag, time(0, 0)).astimezone()
        ev.setAllDay_(True)
        ev.setStartDate_(self._nsdate(begin))
        ev.setEndDate_(self._nsdate(begin))
        ev.setNotes_(notities)
        kalender = self._kalender()
        ev.setCalendar_(kalender)
        ev.setAvailability_(self.EK.EKEventAvailabilityFree)
        ev.addAlarm_(self.EK.EKAlarm.alarmWithRelativeOffset_(-15 * 3600))   # 09:00 de dag ervoor
        slot = self._maak_prive(ev, kalender) if self.prive else False
        ok, fout = self.store.saveEvent_span_error_(ev, self.EK.EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"Deadline niet opgeslagen: {fout}")
        return (f"deadline op {DAGEN[dag.weekday()]} {dag.day} {MAANDEN[dag.month - 1]} in je agenda, "
                f"herinnering de dag ervoor om 09:00" + ("" if slot or not self.prive else "; niet privé"))

    def test_afspraak(self) -> tuple[object, dict]:
        """Testafspraak morgen 07:30 tot 07:45, om herinnering en slotje in Outlook te controleren."""
        morgen = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=7, minute=30, second=0, microsecond=0)
        ev, slot = self._nieuwe_afspraak("Mailagent-test: slotje en herinnering", morgen, morgen + timedelta(minutes=15),
                                         "Testafspraak van mailagent. Mag weg.")
        info = {"start": morgen, "slot": slot, "herinneringen": len(ev.alarms() or [])}
        try:
            info["niveau"] = str(ev.privacyLevelString()) if ev.respondsToSelector_("privacyLevelString") else None
        except Exception:
            info["niveau"] = None
        return ev, info

    def verwijder(self, ev) -> None:
        ok, fout = self.store.removeEvent_span_error_(ev, self.EK.EKSpanThisEvent, None)
        if not ok:
            raise RuntimeError(f"Afspraak niet verwijderd: {fout}")


def maak_agenda(cfg) -> tuple[Optional[Agenda], Optional[str]]:
    """Geeft (agenda, None) of (None, reden waarom er geen agenda is)."""
    ruw = cfg.agenda or {}
    if not ruw.get("kalender"):
        return None, "Geen agenda ingesteld (agenda: kalender: in config.yaml)"
    try:
        herinnering = ruw.get("herinnering_minuten", 15)
        return AppleAgenda(ruw["kalender"], ruw.get("account"), ruw.get("bezet"),
                           herinnering_minuten=int(herinnering) if herinnering else None,
                           prive=bool(ruw.get("prive", True))), None
    except ImportError:
        return None, "Agendakoppeling niet geïnstalleerd: .venv/bin/pip install pyobjc-framework-EventKit"
    except Exception as e:
        return None, str(e)
