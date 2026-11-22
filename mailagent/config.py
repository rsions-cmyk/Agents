"""Configuratie laden en de lokale regels (sorteren, privacy) toepassen."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .models import Email, Sortering


@dataclass
class Config:
    pad: Path
    data_dir: Path
    bronnen: list[dict]
    projecten: list[str]
    project_instructies: dict[str, str]
    regels: list[dict] = field(default_factory=list)
    nooit_van: list[str] = field(default_factory=list)
    nooit_onderwerp: list[str] = field(default_factory=list)
    anonimiseren: list[tuple[re.Pattern, str]] = field(default_factory=list)
    backend: str = "claude-code"
    claude_pad: str = "claude"
    claude_code_model: str = "opus"
    model: str = "claude-opus-5-5"
    sorteren_effort: str = "low"
    uitwerken_effort: str = "medium"
    eerste_run_dagen_terug: int = 2
    max_mails_per_run: int = 60
    agenda: dict = field(default_factory=dict)
    web_host: str = "127.0.0.1"
    web_poort: int = 8765

    @property
    def db_pad(self) -> Path:
        return self.data_dir / "mailagent.sqlite"


def laad(pad: str | Path = "config.yaml") -> Config:
    pad = Path(pad).resolve()
    if not pad.exists():
        raise SystemExit(f"Configuratie niet gevonden: {pad}. Kopieer config.example.yaml naar config.yaml.")
    ruw = yaml.safe_load(pad.read_text(encoding="utf-8")) or {}
    basis = pad.parent

    projecten = ruw.get("projecten") or ["onderwijs", "onderzoek", "istte", "overig"]
    if "overig" not in projecten:
        projecten.append("overig")
    instructies = {}
    for p in projecten:
        bestand = basis / "projecten" / f"{p}.md"
        if not bestand.exists():
            raise SystemExit(f"Instructiebestand ontbreekt: {bestand}")
        instructies[p] = bestand.read_text(encoding="utf-8")

    nooit = ruw.get("nooit_naar_claude") or {}
    claude = ruw.get("claude") or {}
    web = ruw.get("web") or {}
    data_dir = (basis / ruw.get("data_dir", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        pad=pad,
        data_dir=data_dir,
        bronnen=ruw.get("bronnen") or [],
        projecten=projecten,
        project_instructies=instructies,
        regels=ruw.get("regels") or [],
        nooit_van=[s.lower() for s in (nooit.get("van") or []) if s],
        nooit_onderwerp=[s.lower() for s in (nooit.get("onderwerp") or []) if s],
        anonimiseren=[(re.compile(r["patroon"]), r.get("vervang", "[VERWIJDERD]")) for r in (ruw.get("anonimiseren") or [])],
        backend=claude.get("backend", "claude-code"),
        claude_pad=claude.get("pad", "claude"),
        claude_code_model=claude.get("claude_code_model", "opus"),
        model=claude.get("model", "claude-opus-5-5"),
        sorteren_effort=claude.get("sorteren_effort", "low"),
        uitwerken_effort=claude.get("uitwerken_effort", "medium"),
        eerste_run_dagen_terug=int(ruw.get("eerste_run_dagen_terug", 2)),
        max_mails_per_run=int(ruw.get("max_mails_per_run", 60)),
        agenda=ruw.get("agenda") or {},
        web_host=web.get("host", "127.0.0.1"),
        web_poort=int(web.get("poort", 8765)),
    )


def mag_naar_claude(cfg: Config, mail: Email) -> bool:
    van, onderwerp = mail.van.lower(), mail.onderwerp.lower()
    if any(s in van for s in cfg.nooit_van):
        return False
    if any(s in onderwerp for s in cfg.nooit_onderwerp):
        return False
    return True


def anonimiseer(cfg: Config, tekst: str) -> str:
    for patroon, vervang in cfg.anonimiseren:
        tekst = patroon.sub(vervang, tekst)
    return tekst


def sorteer_met_regels(cfg: Config, mail: Email) -> tuple[Optional[str], Optional[Sortering]]:
    """Vaste regels uit config. De eerste regel die past, wint.

    Geeft (project, sortering). Een regel zonder 'actie' legt alleen het project vast
    (sortering is dan None en Claude bepaalt de actie). Past geen regel: (None, None).
    """
    van, onderwerp = mail.van.lower(), mail.onderwerp.lower()
    for regel in cfg.regels:
        if "van" in regel and regel["van"].lower() not in van:
            continue
        if "onderwerp" in regel and regel["onderwerp"].lower() not in onderwerp:
            continue
        project = regel.get("project")
        if project not in cfg.projecten:
            continue
        if "actie" not in regel:
            return project, None
        return project, Sortering(project=project, actie=regel["actie"],
                                  prioriteit=regel.get("prioriteit", "laag"), deadline=None,
                                  samenvatting=mail.onderwerp, reden="vaste regel")
    return None, None
