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


class KeuzeTaak(BaseModel):
    """Het werk dat volgt als de eigenaar deze optie kiest, zoals de review zelf na 'Accept'."""
    omschrijving: str
    minuten: Optional[int] = None
    deadline: Optional[str] = None


class Optie(BaseModel):
    label: str                     # op de knop, Brits Engels
    tekst: Optional[str] = None    # wat op de plek in het concept komt, in de taal van de mail
    taak: Optional[KeuzeTaak] = None   # alleen bij een beslissing over de hele mail


class OpenPunt(BaseModel):
    """Iets wat de eigenaar moet beslissen of aanvullen. 'keuze' en 'invullen' horen bij een [FILL IN: …]-plek
    in het concept; 'hele_mail' verandert het hele antwoord (ja of nee op een verzoek)."""
    vraag: str
    soort: str = "invullen"        # keuze | invullen | hele_mail
    plek: Optional[str] = None     # de letterlijke [FILL IN: …]-tekst in het concept
    opties: list[Optie] = []
    suggestie: Optional[int] = None


class Uitwerking(BaseModel):
    concept_antwoord: Optional[str]
    taken: list[Taak]
    agenda_voorstel: Optional[str]
    vragen_aan_jou: Optional[str]
    open_punten: list[OpenPunt] = []


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
            "description": "Alleen iets voor de eigenaar dat geen keuze of in te vullen detail is; anders null",
        },
        "open_punten": {
            "type": "array",
            "description": "Hoogstens drie punten die de eigenaar moet beslissen of aanvullen, in de volgorde van het concept",
            "items": {
                "type": "object",
                "properties": {
                    "vraag": {"type": "string", "description": "Korte vraag aan de eigenaar, in Brits Engels"},
                    "soort": {"type": "string", "enum": ["keuze", "invullen", "hele_mail"]},
                    "plek": {"type": ["string", "null"],
                             "description": "Letterlijk de [FILL IN: …]-tekst uit het concept die dit punt vervangt; null bij hele_mail"},
                    "opties": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Op de knop, hoogstens vijf woorden, Brits Engels"},
                                "tekst": {"type": ["string", "null"],
                                          "description": "Wat op de plek komt, in de taal van de mail; null bij hele_mail"},
                                "taak": {
                                    "anyOf": [{"type": "null"}, {
                                        "type": "object",
                                        "description": "Alleen bij hele_mail: het werk dat volgt als de eigenaar deze optie kiest",
                                        "properties": {
                                            "omschrijving": {"type": "string", "description": "Wat er moet gebeuren, Brits Engels"},
                                            "minuten": {"type": ["integer", "null"], "description": "Geschat eigen werk in minuten"},
                                            "deadline": {"type": ["string", "null"], "description": "YYYY-MM-DD of null"},
                                        },
                                        "required": ["omschrijving", "minuten", "deadline"],
                                        "additionalProperties": False,
                                    }],
                                },
                            },
                            "required": ["label", "tekst", "taak"],
                            "additionalProperties": False,
                        },
                    },
                    "suggestie": {"type": ["integer", "null"],
                                  "description": "Nummer (vanaf 0) van de optie die het meest voor de hand ligt, of null"},
                },
                "required": ["vraag", "soort", "plek", "opties", "suggestie"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["concept_antwoord", "taken", "agenda_voorstel", "vragen_aan_jou", "open_punten"],
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
