"""Configuratie laden en de lokale regels (sorteren, privacy) toepassen."""

from __future__ import annotations

import re
import hashlib
import hmac
import secrets
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
    terugkijken_dagen: int = 14
    rondes: list = field(default_factory=lambda: ["07:45", "12:45", "16:15"])
    bulk_overslaan: bool = True
    max_mails_per_run: int = 60
    agenda: dict = field(default_factory=dict)
    vlag_aan: bool = True
    vlag_kleur: int = 4
    web_host: str = "127.0.0.1"
    web_poort: int = 8765
    belangrijk: list[str] = field(default_factory=list)   # adressen of domeinen (@istte.org)
    opvolgen_aan: bool = True
    opvolgen_werkdagen: int = 3
    opruimen_na_antwoord: bool = True
    opbergen_na_afhandelen: bool = True
    anonimiseren_aan: bool = True           # menu Settings; uit: alles gaat zonder vervangen naar Claude
    lokaal_aan: bool = False                # menu Settings; aan: eenvoudige taken door een model op je Mac
    lokaal_model: str | None = None
    lokaal_url: str = "http://localhost:11434"
    meldingen_aan: bool = True
    meldingen_minuten: int = 15
    ochtend_aan: bool = True

    @property
    def db_pad(self) -> Path:
        return self.data_dir / "mailagent.sqlite"


KLEUREN = {"rood": 0, "oranje": 1, "geel": 2, "groen": 3, "blauw": 4, "paars": 5, "grijs": 6}


def laad(pad: str | Path = "config.yaml") -> Config:
    pad = Path(pad).resolve()
    if not pad.exists():
        raise SystemExit(f"Configuration not found: {pad}. Copy config.example.yaml to config.yaml.")
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
        terugkijken_dagen=int(ruw.get("terugkijken_dagen", 14)),
        rondes=[str(r) for r in (ruw.get("rondes") or ["07:45", "12:45", "16:15"])],
        bulk_overslaan=bool(ruw.get("nieuwsbrieven_overslaan", True)),
        max_mails_per_run=int(ruw.get("max_mails_per_run", 60)),
        agenda=ruw.get("agenda") or {},
        vlag_aan=bool((ruw.get("markeren") or {}).get("aan", True)),
        vlag_kleur=KLEUREN.get(str((ruw.get("markeren") or {}).get("kleur", "blauw")).lower(), 4),
        web_host=web.get("host", "127.0.0.1"),
        web_poort=int(web.get("poort", 8765)),
        belangrijk=[str(s).strip().lower() for s in (ruw.get("belangrijk") or []) if str(s).strip()],
        opvolgen_aan=bool((ruw.get("opvolgen") or {}).get("aan", True)),
        opvolgen_werkdagen=int((ruw.get("opvolgen") or {}).get("werkdagen", 3)),
        opruimen_na_antwoord=bool(ruw.get("archiveren_na_antwoord", True)),
        opbergen_na_afhandelen=bool(ruw.get("opbergen_na_afhandelen", True)),
        lokaal_model=(ruw.get("lokaal") or {}).get("model"),
        lokaal_url=str((ruw.get("lokaal") or {}).get("url", "http://localhost:11434")),
        meldingen_aan=bool((ruw.get("meldingen") or {}).get("aan", True)),
        meldingen_minuten=max(5, int((ruw.get("meldingen") or {}).get("elke_minuten", 15))),
        ochtend_aan=bool((ruw.get("meldingen") or {}).get("ochtend", True)),
    )


def mag_naar_claude(cfg: Config, mail: Email) -> bool:
    van, onderwerp = mail.van.lower(), mail.onderwerp.lower()
    if any(s in van for s in cfg.nooit_van):
        return False
    if any(s in onderwerp for s in cfg.nooit_onderwerp):
        return False
    return True


# Omkeerbaar anonimiseren (fout 11). Elke gevonden waarde krijgt een eigen code, zoals [NUMMER-a3f9c1]: dezelfde
# waarde steeds dezelfde code, zodat Claude ziet dat twee keer hetzelfde nummer bedoeld is. De app onthoudt in het
# geheugen welke code bij welke waarde hoort en zet in het antwoord van Claude de echte waarde terug. De sleutel is
# per keer dat de app start nieuw, dus de codes zeggen buiten deze app niets.
_ANON_SLEUTEL = secrets.token_bytes(16)
_ANONIEM: dict[str, str] = {}
ANON_CODE = re.compile(r"\[([A-Z][A-Z_]*)-([0-9a-fA-F]{6})\]")


def pas_instellingen_toe(cfg: Config, store) -> None:
    """De keuzes uit het menu Settings (in de database) over de standaard van config.yaml heen."""
    a, l, m = store.instelling("anonimiseren"), store.instelling("lokaal"), store.instelling("lokaal_model")
    if a is not None:
        cfg.anonimiseren_aan = a == "aan"
    if l is not None:
        cfg.lokaal_aan = l == "aan"
    if m:
        cfg.lokaal_model = m


def anonimiseer(cfg: Config, tekst: str) -> str:
    if not getattr(cfg, "anonimiseren_aan", True):
        return tekst
    for patroon, vervang in cfg.anonimiseren:
        label = re.sub(r"[^A-Z_]", "", str(vervang).upper()) or "ANON"

        def code(m, label=label):
            waarde = m.group(0)
            c = f"[{label}-{hmac.new(_ANON_SLEUTEL, waarde.encode(), hashlib.sha256).hexdigest()[:6]}]"
            _ANONIEM[c] = waarde
            return c
        tekst = patroon.sub(code, tekst)
    return tekst


def herstel_anoniem(waarde):
    """De codes van anonimiseer() in tekst, lijsten en woordenboeken terug naar de echte waarde."""
    if isinstance(waarde, str):
        return ANON_CODE.sub(lambda m: _ANONIEM.get(f"[{m.group(1)}-{m.group(2).lower()}]", m.group(0)), waarde)
    if isinstance(waarde, list):
        return [herstel_anoniem(v) for v in waarde]
    if isinstance(waarde, dict):
        return {k: herstel_anoniem(v) for k, v in waarde.items()}
    return waarde


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
