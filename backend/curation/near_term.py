"""
curation/near_term.py : scaffolds for the NEAR-TERM curation features.

Each function here is real, callable, and persists results to the same
tables LIVE features write to. They are NOT wired into the default flow :
operators have to flip the corresponding SURPLUS_FEATURE_* env var to
make the routes reachable.

When a feature graduates from NEAR-TERM to LIVE, the right move is to:

  1. Move its persistence shape (still in Attendee.* JSON columns for now)
     into dedicated typed columns when it's earning its keep.
  2. Wire it into the default scoring / matching path.
  3. Remove the features.is_enabled() gate from its route.

Until then, this file is the holding pen. Everything here is intentionally
small : just enough to validate the data shape end-to-end without
committing to a full implementation.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from .. import models
from . import claude_log


# ─── Stage 1: news / public signal enrichment ────────────────────────

def refresh_news_signal(
    db: Session,
    attendee: models.Attendee,
    *,
    raw_signals: Iterable[dict] | None = None,
) -> dict:
    """Persist a quality-gated news signal payload on the attendee.

    Operators (or a future scheduled job) call this with the raw signal
    feed they want considered : we strip noise, store the deduped set on
    Attendee.news_signal.

    Schema persisted:
      {"signals": [{"kind":"funding|launch|award|job_change",
                     "headline":"...", "url":"...", "ts":"..."}],
       "refreshed_at": "iso8601"}
    """
    accepted: list[dict] = []
    for s in (raw_signals or []):
        kind = (s.get("kind") or "").lower().strip()
        if kind not in ("funding", "launch", "award", "job_change"):
            continue
        headline = (s.get("headline") or "").strip()
        if not headline:
            continue
        accepted.append({
            "kind": kind, "headline": headline[:280],
            "url": (s.get("url") or "").strip()[:400],
            "ts": (s.get("ts") or "").strip()[:32],
        })
    payload = {
        "signals": accepted,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "method": "rule_based",
    }
    attendee.news_signal = json.dumps(payload)
    db.flush()
    return payload


# ─── Stage 1: proprietary recognition cross-reference ────────────────

def cross_reference_recognition(
    db: Session,
    attendees: list[models.Attendee],
    recognition_list: list[dict],
) -> int:
    """Flag every attendee that appears on an org-uploaded recognition list.

    `recognition_list` is a list of {"name": "...", "email": "...",
    "list_name": "..."} entries. Match priority: email -> case-insensitive
    name. Sets Attendee.recognition_flags = JSON list of list_names.

    Returns the count of attendees that picked up at least one flag.
    """
    by_email = {(r.get("email") or "").lower(): r for r in recognition_list
                if (r.get("email") or "").strip()}
    by_name = {(r.get("name") or "").lower(): r for r in recognition_list
               if not (r.get("email") or "").strip()
               and (r.get("name") or "").strip()}
    flagged = 0
    for a in attendees:
        hits: list[str] = []
        e = (a.email or "").lower().strip()
        if e and e in by_email:
            hits.append(by_email[e].get("list_name") or "recognized")
        n = (a.name or "").lower().strip()
        if n and n in by_name:
            hits.append(by_name[n].get("list_name") or "recognized")
        if hits:
            a.recognition_flags = json.dumps(sorted(set(hits)))
            flagged += 1
    db.flush()
    return flagged


# ─── Stage 1: warm-connection signal ─────────────────────────────────

def attach_warm_connection(
    db: Session,
    attendee: models.Attendee,
    *,
    connector_name: str,
    connector_email: str = "",
    strength: float = 0.5,
    note: str = "",
) -> dict:
    """Record that `connector_name` (in the org's network) knows `attendee`.

    Persists on Attendee.warm_connection as
    {"connector": {...}, "strength": 0.0-1.0, "note": "..."}.
    """
    payload = {
        "connector": {"name": connector_name.strip(),
                       "email": connector_email.strip()},
        "strength": max(0.0, min(1.0, float(strength))),
        "note": note.strip()[:1000],
        "method": "rule_based",
    }
    attendee.warm_connection = json.dumps(payload)
    db.flush()
    return payload


# ─── Stage 2: no-show / yield prediction ─────────────────────────────

def predict_no_show(attendee: models.Attendee) -> tuple[float, str]:
    """Tiny rule-based no-show predictor : the LIVE replacement should
    train on real attendance data. Returns (probability, rationale)."""
    p = 0.15  # baseline
    reasons: list[str] = []
    rsvp = (attendee.rsvp_status or "").lower()
    if rsvp == "waitlist":
        p += 0.25
        reasons.append("on waitlist")
    if rsvp == "invited":
        p += 0.40
        reasons.append("invited only, no RSVP yet")
    if rsvp == "rsvp_yes":
        p -= 0.05
        reasons.append("RSVP'd yes")
    if not attendee.email and not attendee.linkedin_url:
        p += 0.15
        reasons.append("no reachable contact")
    p = max(0.02, min(0.95, p))
    return p, ("; ".join(reasons) + ".") if reasons else "Baseline only."


def write_no_show(db: Session, attendee: models.Attendee) -> tuple[float, str]:
    p, rationale = predict_no_show(attendee)
    attendee.no_show_probability = p
    attendee.no_show_rationale = rationale
    db.flush()
    return p, rationale


# ─── Stage 3: sponsor-to-attendee matching ───────────────────────────

def match_sponsor_to_attendees(
    sponsor_profile: dict,
    attendees: list[models.Attendee],
    *,
    top_n: int = 20,
) -> list[dict]:
    """Pure-function sponsor matcher : rank attendees against one sponsor's
    buyer profile. Doesn't persist anything : the route handler decides.

    `sponsor_profile` = {"name": "...", "buyer_function": "Engineering",
    "buyer_seniority": ["Senior", "Staff+"], "industries": [...]}
    """
    from . import enrichment as enrich_mod
    out: list[dict] = []
    for a in attendees:
        e = enrich_mod.get_enrichment(a)
        score = 0.0
        trace: list[str] = []
        if (e.get("role") or {}).get("function") == sponsor_profile.get("buyer_function"):
            score += 0.5
            trace.append("function_match")
        sen = (e.get("seniority") or {}).get("level")
        if sen and sen in (sponsor_profile.get("buyer_seniority") or []):
            score += 0.3
            trace.append(f"seniority_match:{sen}")
        ind = (e.get("firmographic") or {}).get("company_industry") or ""
        for want in sponsor_profile.get("industries") or []:
            if want and want.lower() in ind.lower():
                score += 0.2
                trace.append(f"industry:{ind}")
                break
        if score > 0:
            out.append({
                "attendee_id": a.id, "attendee_name": a.name,
                "score": round(min(1.0, score), 3),
                "rule_trace": trace,
                "method": "rule_based",
                "intro_path": f"{sponsor_profile.get('name', 'sponsor')} -> {a.name}",
            })
    out.sort(key=lambda r: -r["score"])
    return out[:top_n]


# ─── Stage 3: seating optimization (round-robin placeholder) ─────────

def optimize_seating(
    attendees: list[models.Attendee],
    *,
    table_size: int = 6,
) -> dict[int, list[int]]:
    """Side-balanced round-robin into tables of `table_size`. The LIVE
    version should consume curation/intros.py weights as edges.

    Returns {table_id: [attendee_id, ...]}.
    """
    n_tables = max(1, round(len(attendees) / table_size))
    from . import enrichment as enrich_mod
    founders = []
    others = []
    for a in attendees:
        e = enrich_mod.get_enrichment(a)
        fn = (e.get("role") or {}).get("function")
        if fn in ("Founder", "Investor"):
            founders.append(a)
        else:
            others.append(a)
    tables: dict[int, list[int]] = {i: [] for i in range(1, n_tables + 1)}
    for i, a in enumerate(founders):
        tables[(i % n_tables) + 1].append(a.id)
    for i, a in enumerate(others):
        tables[(i % n_tables) + 1].append(a.id)
    return tables


# ─── Stage 3: attendee-to-session relevance ──────────────────────────

def score_attendee_for_session(
    attendee: models.Attendee,
    session: dict,
) -> tuple[float, list[str]]:
    """Rule-based attendee-to-session scoring. `session` is
    {"title": "...", "keywords": [...], "target_function": "Engineering"}.
    """
    from . import enrichment as enrich_mod
    e = enrich_mod.get_enrichment(attendee)
    score = 0.0
    trace: list[str] = []
    fn = (e.get("role") or {}).get("function")
    if fn and fn == session.get("target_function"):
        score += 0.5
        trace.append(f"function_match:{fn}")
    haystack = " ".join([
        attendee.role or "",
        (e.get("role") or {}).get("specialty") or "",
        (e.get("firmographic") or {}).get("company_summary") or "",
    ]).lower()
    for kw in session.get("keywords") or []:
        if kw.lower() in haystack:
            score += 0.15
            trace.append(f"keyword:{kw}")
    return min(1.0, score), trace


# ─── Stage 5: sponsor ROI rollup ─────────────────────────────────────

def sponsor_roi_rollup(
    db: Session,
    event_id: int,
    sponsor_matches: list[dict],
) -> dict:
    """Aggregate AttendeeAttribution rows across the sponsor-matched
    attendees for one event. Pure rollup, no LLM."""
    attendee_ids = {m["attendee_id"] for m in sponsor_matches}
    if not attendee_ids:
        return {"event_id": event_id, "matched_attendees": 0,
                 "outcomes": {}, "total_value": 0}
    rows = (db.query(models.AttendeeAttribution)
              .filter(models.AttendeeAttribution.event_id == event_id,
                      models.AttendeeAttribution.attendee_id.in_(attendee_ids))
              .all())
    by_outcome: dict[str, int] = {}
    total_value = 0
    for r in rows:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
        total_value += r.value or 0
    return {
        "event_id": event_id,
        "matched_attendees": len(attendee_ids),
        "attributed": len(rows),
        "outcomes": by_outcome,
        "total_value": total_value,
        "method": "rule_based",
    }


# ─── Stage 5: news-signal attribution ───────────────────────────────

def news_signal_attribution(attendee: models.Attendee) -> dict:
    """Surface any news signals from refresh_news_signal() as candidate
    attribution evidence. The LIVE version should plug into attribute_attendee
    as additional evidence rather than its own outcome path."""
    try:
        payload = json.loads(attendee.news_signal or "{}")
    except json.JSONDecodeError:
        payload = {}
    signals = payload.get("signals") or []
    evidence: list[dict] = []
    for s in signals:
        if s.get("kind") in ("funding", "launch", "award", "job_change"):
            evidence.append({
                "kind": s["kind"],
                "headline": s.get("headline"),
                "url": s.get("url"),
                "ts": s.get("ts"),
            })
    return {"attendee_id": attendee.id, "evidence_candidates": evidence,
            "method": "rule_based"}


# ─── Stage 5: recurring-event memory ────────────────────────────────

def recurring_memory_for_user(
    db: Session,
    user_id: int,
    *,
    limit: int = 50,
) -> list[dict]:
    """Pull every attendee profile that produced a non-`none` attribution
    across this user's prior events. Output is a list ready for the
    NEXT cycle's scoring step to bias against.
    """
    # Join attendee -> event (user_id), attribution where outcome != "none".
    q = (db.query(models.Attendee, models.AttendeeAttribution)
           .join(models.AttendeeAttribution,
                 models.AttendeeAttribution.attendee_id == models.Attendee.id)
           .join(models.Event, models.Event.id == models.Attendee.event_id)
           .filter(models.Event.user_id == user_id,
                   models.AttendeeAttribution.outcome != "none")
           .order_by(models.AttendeeAttribution.created_at.desc())
           .limit(limit))
    out: list[dict] = []
    for a, attr in q.all():
        out.append({
            "attendee_id": a.id, "name": a.name,
            "role": a.role, "company": a.company,
            "outcome": attr.outcome, "value": attr.value,
            "confidence": attr.confidence,
            "from_event_id": attr.event_id,
            "rationale": attr.rationale,
        })
    return out
