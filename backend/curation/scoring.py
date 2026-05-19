"""
curation/scoring.py : Stage 2 ICP fit scoring.

Two distinct pieces, deliberately separated so the audit trail is honest:

  score_attendee(attendee, icp) -> (score, rule_trace)
      RULE-BASED. Deterministic 0-100 score from the attendee's enrichment
      vs the operator-defined ICP. Returns the list of rules that fired so
      the score is reproducible and auditable WITHOUT an LLM.

  rationale_for(attendee, icp, score, rule_trace) -> str
      OPTIONAL Claude pass that converts the rule trace + enrichment into
      one English sentence. Strictly a UI nicety on top of the deterministic
      score : never overrides the score, never adds new signal.

The brief: "Don't claim AI where the logic is rule-based; label internally."
The score IS rule-based. Only `fit_rationale` is AI, and we audit-log every
call.
"""
from __future__ import annotations
import json
import os
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from . import claude_log, enrichment as enrich_mod


# Seniority ladder used for rank-distance scoring. Mirrors agents/scorer.py
# but extends through the curation seniority set.
_SENIORITY_RANK = {
    "Student": -1, "New grad": 0, "Junior": 1, "Mid": 2,
    "Senior": 3, "Staff+": 4, "Leadership": 5,
}


class ICP:
    """Operator-defined ideal-attendee profile for one event.

    Attributes are CSV-aware : `seniority`, `function`, `company_stage` can
    each be a single string OR a comma-joined list. The matcher reads the
    set, not the string.
    """

    def __init__(
        self,
        role: str = "",
        seniority: str = "",
        function: str = "",
        company_stage: str = "",
        company_industry: str = "",
        company_size_bucket: str = "",
        keywords: Optional[list[str]] = None,
    ):
        self.role = role.strip()
        self.seniority = [s.strip() for s in (seniority or "").split(",") if s.strip()]
        self.function = [s.strip() for s in (function or "").split(",") if s.strip()]
        self.company_stage = [s.strip() for s in (company_stage or "").split(",") if s.strip()]
        self.company_industry = [s.strip() for s in (company_industry or "").split(",") if s.strip()]
        self.company_size_bucket = [s.strip() for s in (company_size_bucket or "").split(",") if s.strip()]
        self.keywords = [k.strip().lower() for k in (keywords or []) if k.strip()]

    @classmethod
    def from_event(cls, event: models.Event) -> "ICP":
        """Derive an ICP from the Event's existing intake fields.

        Events were originally designed for outbound prospecting (role,
        seniority, co_stage). Curation reuses those same fields by treating
        role -> function-hint, seniority -> seniority, co_stage -> company_stage.
        Routes can also POST a more detailed ICP override : see routes/curation.py.
        """
        return cls(
            role=event.role or "",
            seniority=event.seniority or "",
            company_stage=event.co_stage or "",
        )


def _lower(v: Optional[str]) -> str:
    return (v or "").strip().lower()


def score_attendee(attendee: models.Attendee, icp: ICP) -> tuple[int, list[str]]:
    """Return (fit_score, rule_trace).

    Deterministic : same inputs always produce the same score. The trace is
    a list of strings like "seniority_match:Senior" so the rationale step
    has something concrete to verbalize without inventing.
    """
    enrichment = enrich_mod.get_enrichment(attendee)

    score = 40
    trace: list[str] = []

    # --- seniority ---------------------------------------------------------
    canonical_seniority = (
        enrichment.get("seniority", {}).get("level")
        or attendee.seniority
        or ""
    )
    want_levels = icp.seniority
    if want_levels and canonical_seniority:
        want_ranks = [_SENIORITY_RANK[s] for s in want_levels if s in _SENIORITY_RANK]
        have_rank = _SENIORITY_RANK.get(canonical_seniority, 2)
        if want_ranks:
            want_min = min(want_ranks)
            if have_rank >= want_min:
                score += 18
                trace.append(f"seniority_meets_target:{canonical_seniority}")
            elif have_rank == want_min - 1:
                score += 6
                trace.append(f"seniority_one_below:{canonical_seniority}")
            else:
                score -= 8
                trace.append(f"seniority_below:{canonical_seniority}")

    # --- function (engineering/product/etc) -------------------------------
    fn = (enrichment.get("role") or {}).get("function") or ""
    if icp.function and fn:
        if any(_lower(w) == _lower(fn) for w in icp.function):
            score += 14
            trace.append(f"function_match:{fn}")
        else:
            score -= 4
            trace.append(f"function_off:{fn}")

    # --- company stage ----------------------------------------------------
    stage = (enrichment.get("firmographic") or {}).get("company_stage") or ""
    if icp.company_stage and stage:
        if any(_lower(s) == _lower(stage) for s in icp.company_stage):
            score += 8
            trace.append(f"stage_match:{stage}")

    # --- company industry --------------------------------------------------
    industry = (enrichment.get("firmographic") or {}).get("company_industry") or ""
    if icp.company_industry and industry:
        wants = [_lower(x) for x in icp.company_industry]
        if any(w in _lower(industry) or _lower(industry) in w for w in wants):
            score += 6
            trace.append(f"industry_match:{industry}")

    # --- company size ----------------------------------------------------
    size_bucket = (enrichment.get("firmographic") or {}).get("company_size_bucket") or ""
    if icp.company_size_bucket and size_bucket:
        if size_bucket in icp.company_size_bucket:
            score += 4
            trace.append(f"size_match:{size_bucket}")

    # --- keyword match in role/specialty/summary --------------------------
    if icp.keywords:
        haystack = " ".join([
            attendee.role or "",
            (enrichment.get("role") or {}).get("specialty") or "",
            (enrichment.get("firmographic") or {}).get("company_summary") or "",
        ]).lower()
        hits = [k for k in icp.keywords if k in haystack]
        if hits:
            score += min(10, len(hits) * 4)
            trace.append("keyword_hits:" + ",".join(hits))

    # --- contact reachability ---------------------------------------------
    if attendee.email or attendee.linkedin_url:
        score += 4
        trace.append("contact_reachable")
    else:
        score -= 4
        trace.append("no_contact")

    # --- enrichment confidence penalty ------------------------------------
    confidence = enrichment.get("confidence") or "low"
    if confidence == "low":
        score -= 4
        trace.append("low_enrichment_confidence")

    score = max(0, min(100, score))
    return score, trace


_RATIONALE_MODEL = os.environ.get("CURATION_RATIONALE_MODEL", "claude-haiku-4-5-20251001")
_RATIONALE_TIMEOUT_S = float(os.environ.get("CURATION_RATIONALE_TIMEOUT", "8"))

_RATIONALE_SYSTEM = """You are explaining a deterministic ICP-fit score to
an event organizer. The score and the rule trace are already final : your
job is to summarize WHY they came out that way in one sentence.

Strict constraints:
  - One sentence, ≤30 words.
  - Reference rules from the provided trace : never invent new reasons.
  - Don't quote numbers (the operator sees them next to your text).
  - Don't praise or apologize. Neutral, declarative."""


def rationale_for(
    db: Session,
    attendee: models.Attendee,
    icp: ICP,
    score: int,
    trace: list[str],
) -> str:
    """Generate the optional plain-English rationale for a fit score.

    Returns the cached rationale if one already exists. On any LLM failure
    falls back to a deterministic '; '.join(trace) so the score remains
    intelligible without Claude.
    """
    if attendee.fit_rationale and attendee.fit_score == score:
        return attendee.fit_rationale

    fallback = ("; ".join(trace).replace("_", " ") + ".") if trace else "No signal."

    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        claude_log.log_disabled(
            db, purpose="score_rationale",
            event_id=attendee.event_id, attendee_id=attendee.id,
        )
        return fallback

    enrichment = enrich_mod.get_enrichment(attendee)
    user_prompt = (
        f"Score: {score}\n"
        f"Rule trace: {json.dumps(trace)}\n\n"
        f"ICP:\n"
        f"  role: {icp.role}\n"
        f"  seniority: {icp.seniority}\n"
        f"  function: {icp.function}\n"
        f"  company_stage: {icp.company_stage}\n"
        f"  company_industry: {icp.company_industry}\n"
        f"  keywords: {icp.keywords}\n\n"
        f"Attendee enrichment:\n{json.dumps(enrichment, indent=2)}\n\n"
        f"Write the one-sentence rationale now."
    )

    with claude_log.log_call(
        db, purpose="score_rationale", model=_RATIONALE_MODEL,
        event_id=attendee.event_id, attendee_id=attendee.id,
        prompt=f"SYSTEM:\n{_RATIONALE_SYSTEM}\n\nUSER:\n{user_prompt}",
    ) as call:
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=_RATIONALE_MODEL,
                max_tokens=200,
                timeout=_RATIONALE_TIMEOUT_S,
                system=[{"type": "text", "text": _RATIONALE_SYSTEM,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            call.status = "error"
            call.error = f"{type(exc).__name__}: {exc}"
            return fallback

        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        call.output = text
        return text or fallback


def score_and_explain(
    db: Session,
    attendee: models.Attendee,
    icp: ICP,
    *,
    with_rationale: bool = True,
) -> tuple[int, list[str], str]:
    """Score one attendee + optionally generate the LLM rationale. Writes
    everything back to the Attendee row (fit_score, fit_rule_trace,
    fit_rationale)."""
    score, trace = score_attendee(attendee, icp)
    rationale = ""
    if with_rationale:
        rationale = rationale_for(db, attendee, icp, score, trace)

    attendee.fit_score = score
    attendee.fit_rule_trace = json.dumps(trace)
    attendee.fit_rationale = rationale
    return score, trace, rationale
