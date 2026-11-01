"""Phase 4 (F16–F20): one presentation build, five consumers.

:mod:`bike_agent.presentation.analysis` turns the persisted Phase 1 artefacts
into a single analysis object.  The workbench page, the ``presentation.js``
bundle, the guide, the event list, the delta map, the per-cell ledger, the
scenario table and ``scripts/check_presentation.py`` all read that one object
(REQUIREMENTS-PHASE4 §2.3), so there is exactly one derivation of every
"same-named" number.
"""

from __future__ import annotations

from bike_agent.presentation.analysis import (
    EVENT_TYPES,
    build_all_presentations,
    build_presentation,
    presentation_js,
)
from bike_agent.presentation.render import build_workbench

__all__ = [
    "EVENT_TYPES",
    "build_all_presentations",
    "build_presentation",
    "build_workbench",
    "presentation_js",
]
