"""
curation/intros.py : Stage 3 attendee-to-attendee intro recommendations.

Rule-based pairing on complementary signals. We score every directed pair
(A→B) using:

  - cross-function complementarity (Engineering ↔ Product, Founder ↔ Investor, ...)
  - cross-stage complementarity (early-stage founders ↔ later-stage operators)
  - shared specialty / industry (affinity, not symbiosis : softer signal)
  - fit-score proximity (only intro people who both clear a baseline)

Output: AttendeeIntro rows. Each row carries a rule_trace so the operator
can see WHY the pairing was suggested.

The brief calls this "rule-based pairing on complementary profiles/goals."
We deliberately don't invoke Claude to *generate* the recommendation : the
score IS the rule trace. A separate (future) step could synthesize a
natural-language pitch on top, but that would be labeled as AI.

Export: build_intro_list_for(attendee) returns the pre-event intro card
the operator can hand to one attendee.
"""
from __future__ import annotations
import json
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from . import enrichment as enrich_mod


# Cross-function complements : (a, b) means "introduce A (function=a) to
# B (function=b) is valuable". Symmetric pairings are listed both ways
# below so the directed loop catches them.
_COMPLEMENTS: dict[str, set[str]] = {
    "Engineering": {"Product", "Design", "Founder"},
    "Product": {"Engineering", "Design", "Marketing"},
    "Design": {"Engineering", "Product"},
    "Sales": {"Founder", "Operations", "Marketing"},
    "Marketing": {"Sales", "Product"},
    "Operations": {"Founder", "Finance", "Sales"},
    "Finance": {"Founder", "Operations", "Investor"},
    "Founder": {"Investor", "Engineering", "Sales", "Finance"},
    "Investor": {"Founder", "Finance"},
}


# Heuristic stage-direction value : early-stage founders benefit MORE from
# late-stage operators than the other way around. Keep numeric so we can
# blend with affinity.
_STAGE_DIRECTION: dict[tuple[str, str], float] = {
    ("Pre-seed", "Seed"): 0.2,
    ("Pre-seed", "Series A"): 0.4,
    ("Pre-seed", "Series B"): 0.5,
    ("Seed", "Series A"): 0.3,
    ("Seed", "Series B"): 0.4,
    ("Seed", "Series C+"): 0.5,
    ("Series A", "Series B"): 0.2,
    ("Series A", "Series C+"): 0.3,
}


def _function(attendee) -> str | None:
    return (enrich_mod.get_enrichment(attendee).get("role") or {}).get("function")


def _stage(attendee) -> str | None:
    return (enrich_mod.get_enrichment(attendee).get("firmographic") or {}).get("company_stage")


def _industry(attendee) -> str | None:
    return (enrich_mod.get_enrichment(attendee).get("firmographic") or {}).get("company_industry")


def _specialty(attendee) -> str | None:
    return (enrich_mod.get_enrichment(attendee).get("role") or {}).get("specialty")


def score_pair(a: models.Attendee, b: models.Attendee) -> tuple[float, list[str]]:
    """Score a directed (a -> b) intro. Returns (weight 0.0-1.0, rule_trace)."""
    trace: list[str] = []
    weight = 0.0

    fa, fb = _function(a), _function(b)
    if fa and fb and fb in _COMPLEMENTS.get(fa, set()):
        weight += 0.45
        trace.append(f"function_complement:{fa}->{fb}")

    sa, sb = _stage(a), _stage(b)
    if sa and sb:
        directional = _STAGE_DIRECTION.get((sa, sb))
        if directional:
            weight += directional
            trace.append(f"stage_direction:{sa}->{sb}")

    ia, ib = _industry(a), _industry(b)
    if ia and ib and (ia == ib or ia.lower() in (ib or "").lower()):
        weight += 0.15
        trace.append(f"industry_overlap:{ia}")

    spa, spb = _specialty(a), _specialty(b)
    if spa and spb and spa.lower() == spb.lower():
        weight += 0.10
        trace.append(f"specialty_overlap:{spa}")

    # Fit-score floor : both sides need to be reasonable curated picks.
    if a.fit_score >= 60 and b.fit_score >= 60:
        weight += 0.05
        trace.append("both_high_fit")

    return min(1.0, weight), trace


MIN_WEIGHT = 0.30


def build_intros_for_event(
    db: Session,
    event_id: int,
    attendees: list[models.Attendee],
    *,
    min_weight: float = MIN_WEIGHT,
    max_per_attendee: int = 6,
) -> list[models.AttendeeIntro]:
    """Recompute every intro recommendation for one event.

    Idempotent : wipes the prior AttendeeIntro rows for this event first.
    Caller commits.
    """
    # Clear prior rows. fetch synchronization tells the session to evict
    # the deleted rows from the identity map; without it, re-inserts with
    # the same id raise SAWarning on flush.
    db.query(models.AttendeeIntro).filter(
        models.AttendeeIntro.event_id == event_id
    ).delete(synchronize_session="fetch")
    db.flush()

    out: list[models.AttendeeIntro] = []
    # Bucket per-attendee so we can cap per-attendee outbound recs.
    per_from: dict[int, list[tuple[float, list[str], int]]] = {}
    for a in attendees:
        for b in attendees:
            if a.id == b.id:
                continue
            weight, trace = score_pair(a, b)
            if weight < min_weight:
                continue
            per_from.setdefault(a.id, []).append((weight, trace, b.id))

    for from_id, candidates in per_from.items():
        # Highest-weight intros for this attendee first.
        candidates.sort(key=lambda t: -t[0])
        for weight, trace, to_id in candidates[:max_per_attendee]:
            intro = models.AttendeeIntro(
                event_id=event_id,
                from_attendee_id=from_id,
                to_attendee_id=to_id,
                weight=round(weight, 3),
                rule_trace=json.dumps(trace),
                reason="",  # rule-based: deliberately no AI claim here
            )
            db.add(intro)
            out.append(intro)
    db.flush()
    return out


def export_intro_card(
    db: Session,
    event_id: int,
    attendee_id: int,
) -> dict:
    """Build the pre-event intro card for one attendee.

    Returns the JSON payload the operator can paste into a brief, plus a
    machine-readable list of intros for the frontend to render.
    """
    attendee = db.get(models.Attendee, attendee_id)
    if not attendee or attendee.event_id != event_id:
        return {"error": "attendee not found on event"}

    intros = (db.query(models.AttendeeIntro)
                .filter(models.AttendeeIntro.event_id == event_id,
                        models.AttendeeIntro.from_attendee_id == attendee_id)
                .order_by(models.AttendeeIntro.weight.desc())
                .all())

    rows: list[dict] = []
    for intro in intros:
        target = db.get(models.Attendee, intro.to_attendee_id)
        if not target:
            continue
        try:
            trace = json.loads(intro.rule_trace or "[]")
        except json.JSONDecodeError:
            trace = []
        rows.append({
            "to_attendee_id": target.id,
            "to_name": target.name,
            "to_role": target.role,
            "to_company": target.company,
            "weight": intro.weight,
            "rule_trace": trace,
            "method": "rule_based",
        })

    return {
        "attendee_id": attendee.id,
        "attendee_name": attendee.name,
        "intro_count": len(rows),
        "intros": rows,
    }
