"""
curation/gap_analysis.py : Stage 2 ideal-distribution delta.

The brief: "compute the delta between the ideal profile distribution and
the current list; surface who's missing."

Inputs:
  - The Event's ICP (`scoring.ICP`)
  - The Event's current Attendees (with their enrichment payloads)
  - A target distribution the operator can set: e.g. {function: {"Engineering": 0.4, "Product": 0.3, ...}}

Output is intentionally interpretable, not a single score:

  {
    "buckets": {
      "function": {
        "target": {"Engineering": 12, "Product": 8, ...},   # absolute counts derived from headcount
        "actual": {"Engineering": 9, "Product": 12, ...},
        "deficit": {"Engineering": 3},                       # buckets we're short on
        "surplus": {"Product": 4},
      },
      "seniority": {...},
      ...
    },
    "summary": "Engineering deficit of 3; Product surplus of 4; ...",
    "ideal_signals_missing": ["Investor function entirely absent", ...]
  }

Rule-based throughout : labelled internally so we don't claim AI here.
"""
from __future__ import annotations
import math
from typing import Any

from .. import models
from . import enrichment as enrich_mod


# Bucket name -> getter function on (attendee, enrichment) -> str | None
def _function(_attendee, enrichment) -> str | None:
    return (enrichment.get("role") or {}).get("function")


def _seniority(attendee, enrichment) -> str | None:
    return (enrichment.get("seniority") or {}).get("level") or attendee.seniority


def _stage(_attendee, enrichment) -> str | None:
    return (enrichment.get("firmographic") or {}).get("company_stage")


def _industry(_attendee, enrichment) -> str | None:
    return (enrichment.get("firmographic") or {}).get("company_industry")


BUCKETS = {
    "function": _function,
    "seniority": _seniority,
    "company_stage": _stage,
    "company_industry": _industry,
}


def _target_counts(target_dist: dict[str, float], headcount: int) -> dict[str, int]:
    """Convert a fractional target distribution into rounded absolute counts.

    Sums may differ from `headcount` by 1-2 rows when distributions don't
    divide cleanly : that's fine, the gap report is a guide not a ledger."""
    total = sum(target_dist.values()) or 1.0
    return {k: max(0, math.floor(v / total * headcount + 0.5))
            for k, v in target_dist.items()}


def compute_gap(
    event: models.Event,
    attendees: list[models.Attendee],
    target_distributions: dict[str, dict[str, float]],
    *,
    headcount_override: int | None = None,
) -> dict:
    """Run the bucket-by-bucket gap analysis. See module docstring for shape.

    `target_distributions` is keyed by bucket name (must be in BUCKETS) and
    each value is a {label -> fraction-or-count} dict. We treat the values
    as proportional weights : they don't have to sum to 1.0.
    """
    headcount = headcount_override or event.headcount or len(attendees) or 1

    out_buckets: dict[str, dict[str, Any]] = {}
    summary_parts: list[str] = []
    ideal_missing: list[str] = []

    for bucket_name, target in target_distributions.items():
        getter = BUCKETS.get(bucket_name)
        if getter is None or not isinstance(target, dict) or not target:
            continue

        actual: dict[str, int] = {}
        for a in attendees:
            enrich = enrich_mod.get_enrichment(a)
            label = getter(a, enrich)
            if not label:
                actual["(unknown)"] = actual.get("(unknown)", 0) + 1
                continue
            actual[label] = actual.get(label, 0) + 1

        target_counts = _target_counts(target, headcount)
        deficit: dict[str, int] = {}
        surplus: dict[str, int] = {}
        for label, want in target_counts.items():
            have = actual.get(label, 0)
            if have < want:
                deficit[label] = want - have
                if have == 0:
                    ideal_missing.append(
                        f"{bucket_name}:{label} entirely absent (want {want})"
                    )
            elif have > want:
                surplus[label] = have - want

        out_buckets[bucket_name] = {
            "target": target_counts,
            "actual": actual,
            "deficit": deficit,
            "surplus": surplus,
        }
        for label, gap in deficit.items():
            summary_parts.append(f"{bucket_name}:{label} short by {gap}")
        for label, over in surplus.items():
            summary_parts.append(f"{bucket_name}:{label} over by {over}")

    return {
        "headcount": headcount,
        "attendees_seen": len(attendees),
        "buckets": out_buckets,
        "summary": "; ".join(summary_parts) if summary_parts else "no gaps",
        "ideal_signals_missing": ideal_missing,
        "method": "rule_based",  # explicit : not AI
    }
