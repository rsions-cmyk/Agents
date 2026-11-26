"""Meldingen in het Berichtencentrum van je Mac (via osascript; komen van Scripteditor)."""

from __future__ import annotations

import subprocess


def meld(titel: str, tekst: str) -> None:
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv) subtitle "Concierge" sound name "Glass"\nend run'
    try:
        subprocess.run(["osascript", "-e", script, titel[:120], tekst[:240]], check=False, timeout=20,
                       capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        pass
