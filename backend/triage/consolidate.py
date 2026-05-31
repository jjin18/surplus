"""
triage/consolidate.py : deterministic final decision.

The last layer. It combines:
  - the scorer's dimensions + the deterministic confidence floor  (via finalize)
  - Judge B's audit                                               (VerifyResult)

into one verdict. The LLMs never decide here — this module does, by rules. The
auditor can only ever make the outcome MORE conservative (lower confidence,
block an accept, force review); it can never raise a score or upgrade a verdict.
That asymmetry is the safety property: a confused auditor degrades to "needs
human", never to a false accept.

Hard gates (applied upstream as fit caps by the scorer) are never overridden
here — we only touch confidence and the accept/review boundary.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from .recommend import (
    finalize, recommendation_from, apply_archetype_priority,
    Thresholds, RecommendationOutput,
)
from .verify_score import VerifyResult


# When the auditor flags unsupported evidence / a rejected-candidate error but
# gives no explicit cap, this is the hard ceiling we drop confidence to. Chosen
# below every event's accept_confidence_min so a flagged applicant cannot accept.
_FLAGGED_CONFIDENCE_CEILING = 50


@dataclass(frozen=True)
class FinalDecision:
    fit_score: int
    confidence_score: int
    recommendation: str
    verifier_ran: bool = False
    adjustments: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "fit_score": self.fit_score,
            "confidence_score": self.confidence_score,
            "recommendation": self.recommendation,
            "verifier_ran": self.verifier_ran,
            "adjustments": list(self.adjustments),
        }


def consolidate(applicant, dimension_scores: dict, llm_confidence: int,
                *, weights: Optional[dict] = None,
                thresholds: Optional[Thresholds] = None,
                verify: Optional[VerifyResult] = None,
                archetype: str = "",
                founder_corroborated: bool = False,
                priority_policy: Optional[dict] = None) -> FinalDecision:
    """Produce the final (fit, confidence, recommendation), applying the audit.

    `verify` is None when Judge B was skipped (clean applicant) — in that case
    this is exactly finalize() wrapped. When present, the audit can only lower
    confidence and/or downgrade the verdict; it never raises either.

    `priority_policy` (from triage_config) lets the operator make archetype
    priority STRUCTURAL — e.g. boost corroborated founders, cap pure investors —
    instead of relying on rubric prose. It is applied deterministically to the
    fit score BEFORE the verdict is derived, so the audit's conservatism still
    layers on top. With no policy this is a no-op and behaviour is unchanged."""
    base: RecommendationOutput = finalize(
        applicant, dimension_scores, llm_confidence=llm_confidence,
        weights=weights, thresholds=thresholds)
    t = thresholds or Thresholds.default()

    # Data-driven archetype priority (founder boost / investor cap). Applied to
    # fit first so the recommendation in BOTH paths reflects it.
    adj_fit, priority_reasons = apply_archetype_priority(
        base.fit_score, archetype,
        founder_corroborated=founder_corroborated, policy=priority_policy)

    if verify is None or not verify.ran:
        return FinalDecision(
            fit_score=adj_fit,
            confidence_score=base.confidence_score,
            recommendation=recommendation_from(
                adj_fit, base.confidence_score, thresholds=t),
            verifier_ran=False,
            adjustments=tuple(priority_reasons),
        )

    fit = adj_fit
    confidence = base.confidence_score
    adjustments: list[str] = list(priority_reasons)
    block_accept = False

    # 1. Explicit cap from the auditor (it judged confidence overstated).
    if verify.recommended_confidence_cap is not None and \
            verify.recommended_confidence_cap < confidence:
        confidence = verify.recommended_confidence_cap
        adjustments.append(f"confidence capped to {confidence} by auditor")

    # 2. Unsupported evidence or a rejected-candidate error → hard ceiling +
    #    cannot accept. This is the core hallucination guard.
    if verify.used_rejected_candidate:
        block_accept = True
        if confidence > _FLAGGED_CONFIDENCE_CEILING:
            confidence = _FLAGGED_CONFIDENCE_CEILING
        adjustments.append("scorer used a rejected company candidate → accept blocked")
    if verify.unsupported_claims:
        block_accept = True
        if confidence > _FLAGGED_CONFIDENCE_CEILING:
            confidence = _FLAGGED_CONFIDENCE_CEILING
        adjustments.append(
            f"{len(verify.unsupported_claims)} unsupported claim(s) → accept blocked")

    # 3. HARD failures — serious enough to pull even a 'maybe' to a human:
    #      - fail-closed: the audit itself errored / was unparseable (we set
    #        `error` in that path), so we cannot trust it at all.
    #      - the scorer leaned on a REJECTED company candidate (critical grounding
    #        error — it praised the wrong company).
    #      - a contradiction in the packet the scorer missed.
    #    Note: a *successful* audit returning audit_pass=false is NOT a hard
    #    failure on its own — its concrete findings (caps, unsupported claims)
    #    already lowered confidence above. Treating every audit_pass=false as a
    #    forced review is what drowned the queue; the specific findings carry the
    #    signal, not the boolean.
    fail_closed = verify.error is not None
    hard_failure = (fail_closed
                    or verify.used_rejected_candidate
                    or bool(verify.missed_contradictions))
    if fail_closed:
        adjustments.append("audit failed closed (error) → manual review")
    if verify.missed_contradictions:
        adjustments.append(
            f"{len(verify.missed_contradictions)} missed contradiction(s)")
    if verify.force_manual_review and not fail_closed:
        adjustments.append("auditor requested manual review")

    # Recompute the verdict from the (possibly lowered) confidence, then apply
    # the downgrade rules. The auditor only ever pulls a POSITIVE verdict toward
    # needs_review — it never touches a reject (that would be noise AND backwards:
    # needs_review is less conservative than reject on the accept axis).
    #   accept → review on ANY blocking flag (we never auto-admit a questioned accept)
    #   maybe  → review only on a HARD failure (soft caps just leave it a 'maybe')
    recommendation = recommendation_from(fit, confidence, thresholds=t)
    if recommendation == "accept":
        if block_accept or hard_failure or verify.force_manual_review:
            recommendation = "needs_review"
    elif recommendation == "maybe":
        if hard_failure:
            recommendation = "needs_review"

    return FinalDecision(
        fit_score=fit,
        confidence_score=confidence,
        recommendation=recommendation,
        verifier_ran=True,
        adjustments=tuple(adjustments),
    )
