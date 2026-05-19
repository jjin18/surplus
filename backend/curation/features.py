"""
curation/features.py : feature flags for NEAR-TERM curation features.

LIVE features are always on. NEAR-TERM features are scaffolded but gated
behind env-var flags so they don't show up in the default UI until
explicitly enabled. Each flag is an env var of the form

    SURPLUS_FEATURE_<UPPER_NAME>=1

Truthy values: "1", "true", "yes", "on" (case-insensitive). Anything else
(including unset) is OFF.

Why env vars and not a DB table:
  - Per-deploy gating beats per-row : every operator on this build sees the
    same flag state, so we don't have to migrate UI / API contracts mid-run.
  - The Railway dashboard already gives operators a place to flip flags.
  - Tests can monkeypatch os.environ to exercise both branches.

Reference for the LIVE/NEAR-TERM split is the brief in
ARCHITECTURE/curation-spec; mirrored here for in-code discoverability.
"""
from __future__ import annotations
import os
from typing import Literal


# All NEAR-TERM features. Names are stable : route handlers and frontend
# code key off them.
NEAR_TERM_FEATURES: tuple[str, ...] = (
    # Stage 1
    "news_signal",                 # public news / funding / launch enrichment
    "proprietary_recognition",     # cross-reference against org list
    "warm_connection",             # who in your network knows the attendee
    # Stage 2
    "yield_prediction",            # no-show prediction
    # Stage 3
    "sponsor_match",               # sponsor-buyer matching
    "seating_optimization",        # dinner / table / seating optimizer
    "session_relevance",           # attendee-to-session scoring
    # Stage 5
    "sponsor_roi",                 # sponsor-matched outcome rollup
    "news_attribution",            # post-event signal as outcome evidence
    "recurring_memory",            # persist attendee outcome history across events
)

FeatureName = Literal[
    "news_signal", "proprietary_recognition", "warm_connection",
    "yield_prediction", "sponsor_match", "seating_optimization",
    "session_relevance", "sponsor_roi", "news_attribution",
    "recurring_memory",
]


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def is_enabled(feature: str) -> bool:
    """Return True iff the given NEAR-TERM feature is gated on.

    Unknown feature names return False (fail-closed : a typo in a route
    handler shouldn't accidentally enable something).
    """
    if feature not in NEAR_TERM_FEATURES:
        return False
    return _truthy(os.environ.get(f"SURPLUS_FEATURE_{feature.upper()}"))


def all_flags() -> dict[str, bool]:
    """Snapshot of every NEAR-TERM flag's current state. Used by the
    /features endpoint and by tests to assert a clean default."""
    return {name: is_enabled(name) for name in NEAR_TERM_FEATURES}


def require(feature: str) -> None:
    """Raise FastAPI 404 if the feature is off.

    404 (not 403) matches the rest of the codebase's posture : an attacker
    scanning shouldn't learn which gated features exist.
    """
    if not is_enabled(feature):
        from fastapi import HTTPException
        raise HTTPException(404, "Not Found")
