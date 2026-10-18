"""hermes-a2a-admin — Agent-Haelfte des Plugins.

Stufe 1 ist **read-only**: keine Tools, keine Hooks, keine Faehigkeiten. Die
Oberflaechen (``dashboard/``, ``desktop/``) sprechen den Backend-Namespace
``/api/plugins/hermes-a2a-admin/``; die Routen liegen in
``dashboard/plugin_api.py`` und werden vom Dashboard-Wirt geladen
(``manifest.json`` -> ``"api"``).

Der Plugin-Loader verlangt dieses Modul im Ordner — ohne ``__init__.py`` bricht
er mit "No __init__.py" ab (belegt bei search-fanout, 03-10-2026).
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Nichts anzumelden: die Arbeit steckt in den beiden Oberflaechen."""
    return None
