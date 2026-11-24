"""Datamodellen voor mails, sorteerresultaten en uitwerkingen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

Actie = Literal["antwoorden", "taak", "agenda", "ter_info", "delegeren", "negeren"]
Prioriteit = Literal["hoog", "normaal", "laag"]

# Acties waarvoor de projectagent een uitwerking maakt.
UITWERK_ACTIES = {"antwoorden", "taak", "agenda", "delegeren"}


@dataclass
class Email:
    bron: str            # naam van de bron uit config (bijv. "buas")
    bron_id: str         # id van het bericht in die bron
    van: str
    aan: str
    onderwerp: str
    datum: datetime
    tekst: str
    thread_id: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def sleutel(self) -> str:
        return f"{self.bron}:{self.bron_id}"


class Sortering(BaseModel):
    project: str
    actie: Actie
    prioriteit: Prioriteit
    deadline: Optional[str]
    samenvatting: str
    reden: str


class Taak(BaseModel):
    omschrijving: str
    deadline: Optional[str]
    minuten: Optional[int] = None   # geschat eigen werk; oudere items hebben dit niet


class Uitwerking(BaseModel):
    concept_antwoord: Optional[str]
    taken: list[Taak]
    agenda_voorstel: Optional[str]
    vragen_aan_jou: Optional[str]


class Taakplan(BaseModel):
    titel: str
    stappen: list[str]
    minuten: int
    deadline: Optional[str]
    toelichting: str


SCHEMA_TAAKPLAN = {
    "type": "object",
    "properties": {
        "titel": {"type": "string", "description": "Korte taaknaam in de gebiedende wijs, Nederlands"},
        "stappen": {"type": "array", "items": {"type": "string"}, "description": "1 tot 6 concrete stappen"},
        "minuten": {"type": "integer", "description": "Realistische tijd voor het eigen werk van de eigenaar, 15 tot 480"},
        "deadline": {"type": ["string", "null"], "description": "JJJJ-MM-DD als er een deadline uit de mail blijkt, anders null"},
        "toelichting": {"type": "string", "description": "Eén zin: waarom deze tijdsinschatting"},
    },
    "required": ["titel", "stappen", "minuten", "deadline", "toelichting"],
    "additionalProperties": False,
}


def schema_sortering(projecten: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "project": {"type": "string", "enum": projecten},
            "actie": {"type": "string", "enum": list(Actie.__args__)},
            "prioriteit": {"type": "string", "enum": list(Prioriteit.__args__)},
            "deadline": {"type": ["string", "null"], "description": "JJJJ-MM-DD als er een deadline in de mail staat, anders null"},
            "samenvatting": {"type": "string", "description": "Eén zin, Nederlands"},
            "reden": {"type": "string", "description": "Korte uitleg van project en actie"},
        },
        "required": ["project", "actie", "prioriteit", "deadline", "samenvatting", "reden"],
        "additionalProperties": False,
    }


SCHEMA_UITWERKING = {
    "type": "object",
    "properties": {
        "concept_antwoord": {
            "type": ["string", "null"],
            "description": "Volledige tekst van het concept-antwoord zonder onderwerpregel, of null als er geen antwoord nodig is",
        },
        "taken": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "omschrijving": {"type": "string"},
                    "deadline": {"type": ["string", "null"]},
                    "minuten": {"type": ["integer", "null"],
                                "description": "Geschatte minuten eigen werk voor deze taak, afgerond op 15"},
                },
                "required": ["omschrijving", "deadline", "minuten"],
                "additionalProperties": False,
            },
        },
        "agenda_voorstel": {"type": ["string", "null"]},
        "vragen_aan_jou": {
            "type": ["string", "null"],
            "description": "Beslissingen die alleen de eigenaar kan nemen",
        },
    },
    "required": ["concept_antwoord", "taken", "agenda_voorstel", "vragen_aan_jou"],
    "additionalProperties": False,
}


class TaakOordeel(BaseModel):
    taak_id: int
    klaar: bool
    reden: str


class TakenOordeel(BaseModel):
    taken: list[TaakOordeel]


SCHEMA_TAKEN_OORDEEL = {
    "type": "object",
    "properties": {
        "taken": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "taak_id": {"type": "integer"},
                    "klaar": {"type": "boolean", "description": "true alleen als de verstuurde tekst duidelijk laat zien dat de taak gedaan is"},
                    "reden": {"type": "string", "description": "Eén korte zin, in het Nederlands"},
                },
                "required": ["taak_id", "klaar", "reden"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["taken"],
    "additionalProperties": False,
}


class Verwachting(BaseModel):
    nr: int
    verwacht_antwoord: bool
    wat: str


class Verwachtingen(BaseModel):
    mails: list[Verwachting]


SCHEMA_VERWACHTINGEN = {
    "type": "object",
    "properties": {
        "mails": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nr": {"type": "integer"},
                    "verwacht_antwoord": {"type": "boolean",
                                          "description": "true als de eigenaar iets vraagt of verzoekt waarop de ontvanger moet reageren"},
                    "wat": {"type": "string", "description": "Waarop hij wacht, in hoogstens acht woorden, Nederlands; leeg als er niets verwacht wordt"},
                },
                "required": ["nr", "verwacht_antwoord", "wat"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["mails"],
    "additionalProperties": False,
}
