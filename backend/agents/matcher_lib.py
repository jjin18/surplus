"""
agents/matcher_lib.py — bridge from surplus's matcher to the vendored
`backend.matching` library (the real AI-driven matcher).

Surplus already has Prospects in the DB after /prospect runs. The library
expects `EnrichedPerson` dataclasses. This module:

  1. Maps surplus's `Prospect` ORM rows → library's `Person`
  2. Runs library `enrich_batch` (LLM + web_search per person; cached)
  3. Runs library `synthesize_rubric` for the event
  4. Runs library `compute_matrix` to score every pair
  5. Returns the matrix + a Prospect.id → top-K-pair-ids map ready for the
     surplus group-formation step to consume

Output stays compatible with `backend.agents.matcher.build_edges` shape so
the route handler doesn't need to change — same edge dicts, same group
formation, just better-weighted edges driven by the library's composite
score instead of `(avg_fit ± const)`.

Gated on `ANTHROPIC_API_KEY`. When the key is missing this module is
inert and `matcher.build_edges` falls back to the existing heuristic.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from ..matching.enrich import enrich_batch
from ..matching.matrix import compute_matrix
from ..matching.rubric import synthesize_rubric
from ..matching.schema import EnrichedPerson, Person


def library_available() -> bool:
    """True when ANTHROPIC_API_KEY is set — the library needs it for both
    enrichment and rubric synthesis. Returns False on any missing dep."""
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


# In-process cache so form_groups can reuse the matrix that build_edges
# just computed without re-running the library (which is async + slow).
# Keyed by (event_id, frozenset of attending prospect ids).
_MATRIX_CACHE: dict[tuple, dict[str, Any]] = {}


def _cache_key(event, attending: list) -> tuple:
    return (event.id, frozenset(p.id for p in attending))


def get_cached_matrix(event, attending: list) -> Optional[dict[str, Any]]:
    return _MATRIX_CACHE.get(_cache_key(event, attending))


# ---- adapter: surplus Prospect → library Person ---------------------------

# Map surplus's market `side` field to the library's `ticket_type` enum.
# The rubric synthesizer reads ticket_type to decide pair-type weights, so
# the mapping should communicate role intent, not raw side labels.
_SIDE_TO_TICKET = {
    "Builds":   "Attendee",     # builders go in as general attendees
    "Hires":    "Hiring Lead",  # hirer side — looking to add to team
    "Operates": "Founder",      # operators are usually founders / GTM ops
}

_SENIORITY_TO_EXP = {
    "Mid":        "intermediate",
    "Senior":     "advanced",
    "Staff+":     "expert",
    "Leadership": "expert",
}


def prospect_to_person(p) -> Person:
    """Map a surplus Prospect ORM row to a library Person dataclass.

    Identifier fields not stored on Prospect (x_handle, github_username,
    email) are left blank — the library's enrichment step won't have those
    inputs to work with, but it can still scrape from linkedin_url and the
    GitHub API will skip when no username is provided.
    """
    return Person(
        id=f"prospect-{p.id}",
        name=p.name or f"Prospect {p.id}",
        role=p.role or "",
        title=p.role or "",
        company=p.company or "",
        linkedin_url=p.linkedin_url or "",
        ticket_type=_SIDE_TO_TICKET.get(p.side, "Attendee"),
        exp_level=_SENIORITY_TO_EXP.get(p.seniority, "unknown"),
    )


# ---- main entry -----------------------------------------------------------

def score_attendees(attending: list, event) -> Optional[dict[str, Any]]:
    """
    Run the library pipeline against `attending` (list of Prospect ORM rows)
    in the context of `event` (the surplus Event row).

    Returns the matrix dict from `compute_matrix`, or None if the library is
    unavailable / a step failed. The caller (matcher.build_edges) falls back
    to the existing heuristic on None.
    """
    if not library_available() or len(attending) < 2:
        return None
    try:
        people = [prospect_to_person(p) for p in attending]
        event_name = (
            f"{event.format} · {event.headcount}-person · "
            f"{event.city} · goal: {event.goal}"
        )
        event_desc = (
            f"A {event.format.lower()} in {event.city} for "
            f"{event.seniority} {event.role}. The hosting "
            f"organization is at the {event.co_stage} stage. The "
            f"goal is a {event.goal.lower()}. Budget: ${event.budget:,}."
        )

        # The library is fully async — drive it from a fresh event loop so
        # we can call it from the synchronous route handler.
        async def _run() -> dict[str, Any]:
            rubric = await synthesize_rubric(event_name, event_desc, people)
            enriched: list[EnrichedPerson] = await enrich_batch(people)
            return compute_matrix(enriched, rubric, top_k=min(8, len(people) - 1))

        matrix = asyncio.run(_run())
        # Cache so form_groups can reuse without re-calling the library.
        _MATRIX_CACHE[_cache_key(event, attending)] = matrix
        print(f"  [matcher_lib] library scored {len(matrix.get('pairs', []))} pairs "
              f"({len(matrix.get('mutual_pairs', []))} mutual)")
        return matrix
    except Exception as exc:  # noqa: BLE001
        print(f"  [matcher_lib] library scoring failed, falling back: "
              f"{type(exc).__name__}: {exc}")
        return None


# ---- edge builder using library scores -----------------------------------

def build_edges_from_matrix(matrix: dict[str, Any], attending: list) -> list[dict]:
    """
    Turn the library's pair scores into surplus's edge dicts.

    Library output for each pair:
      {
        a_id, b_id, composite (0..1), similar, complementary,
        role_pair, gate_passed, anti_multiplier, ...
      }

    Surplus edge dict:
      {a_id: int, b_id: int, edge_type: "symbiotic"|"affinity", weight: float}

    Heuristic for edge_type from the library output:
      - if role_pair_score is high (the pair is across complementary roles)
        AND complementary axis dominates → "symbiotic"
      - else (mostly similar axis) → "affinity"

    Weight is `composite * 100` so the scale matches the old heuristic's
    0-100ish range (form_groups doesn't read weight, but UI does).
    """
    edges: list[dict] = []
    # Map library person_id ("prospect-42") -> surplus prospect.id (42)
    id_lookup = {f"prospect-{p.id}": p.id for p in attending}

    for pair in matrix.get("pairs", []):
        if not pair.get("gate_passed", True):
            continue
        if pair.get("composite", 0) <= 0:
            continue
        a_id = id_lookup.get(pair["a_id"])
        b_id = id_lookup.get(pair["b_id"])
        if a_id is None or b_id is None:
            continue
        # Decide symbiotic vs affinity from which axis carried the score.
        similar = pair.get("similar", 0)
        complement = pair.get("complementary", 0)
        edge_type = "symbiotic" if complement > similar else "affinity"
        edges.append({
            "a_id": a_id,
            "b_id": b_id,
            "edge_type": edge_type,
            "weight": round(pair["composite"] * 100, 1),
        })
    return edges


# ---- LLM-driven group formation ------------------------------------------
# Replaces matcher.form_groups' round-robin packing with a greedy algorithm
# that maximizes the sum of library-derived composite scores within each
# group, with a soft penalty against same-side concentration.

# Side-imbalance penalty per duplicate side already in the group (0..1).
# Tuned so the LLM score is the dominant signal but a perfectly one-sided
# group is still discouraged.
_SIDE_PENALTY = 0.15


def _pair_score_map(matrix: dict[str, Any]) -> dict[frozenset, float]:
    """Flatten matrix['pairs'] into {frozenset({a_id, b_id}): composite}.

    Uses the *prospect* ids (ints), not library person ids — caller already
    holds Prospect rows, so the inner code should never have to think about
    the "prospect-42" string form again.
    """
    out: dict[frozenset, float] = {}
    for pair in matrix.get("pairs", []):
        if not pair.get("gate_passed", True):
            continue
        comp = pair.get("composite", 0)
        if comp <= 0:
            continue
        a = pair["a_id"]
        b = pair["b_id"]
        # ids are "prospect-N" — strip the prefix back to int
        try:
            ai = int(a.split("-", 1)[1])
            bi = int(b.split("-", 1)[1])
        except (ValueError, IndexError):
            continue
        out[frozenset({ai, bi})] = comp
    return out


def form_groups_from_matrix(attending: list, matrix: dict[str, Any],
                            group_size: int) -> Optional[dict[int, list]]:
    """Greedy LLM-driven group assignment.

    Strategy:
      1. Seed each group with the highest-scoring mutual pair that doesn't
         share a member with an already-seeded group.
      2. While unseated prospects remain, place each into the group that
         maximizes  sum(composite to existing members) − side_penalty.
         The side penalty is small enough that LLM signal dominates, but
         non-zero so one-side groups are mildly discouraged.

    Returns dict[group_id, list[Prospect]] or None if matrix has no usable
    pairs (caller falls back to round-robin).
    """
    if len(attending) < 2:
        return None
    pair_scores = _pair_score_map(matrix)
    if not pair_scores:
        return None

    n = len(attending)
    n_groups = max(1, round(n / group_size))
    groups: dict[int, list] = {i: [] for i in range(1, n_groups + 1)}
    by_id = {p.id: p for p in attending}
    seated: set[int] = set()

    # --- 1) Seed each group with a disjoint top mutual pair (or top pair) ---
    # Prefer mutual pairs (each in the other's top-K) when the library
    # marked any; fall back to plain pair-score order.
    mutual_ids = {frozenset({_strip(m["a_id"]), _strip(m["b_id"])})
                  for m in matrix.get("mutual_pairs", [])
                  if _strip(m.get("a_id")) is not None
                  and _strip(m.get("b_id")) is not None}
    ranked_pairs = sorted(
        pair_scores.items(),
        key=lambda kv: (kv[0] in mutual_ids, kv[1]),
        reverse=True,
    )
    for gid in range(1, n_groups + 1):
        for pair, _score in ranked_pairs:
            a, b = tuple(pair)
            if a in seated or b in seated:
                continue
            groups[gid].extend([by_id[a], by_id[b]])
            seated.update({a, b})
            break

    # --- 2) Greedy fill ---------------------------------------------------
    remaining = [p for p in attending if p.id not in seated]
    # Process highest-fit prospects first so they get first pick of groups.
    remaining.sort(key=lambda p: -getattr(p, "fit_score", 0))

    cap = max(group_size, (n + n_groups - 1) // n_groups)

    for p in remaining:
        best_gid, best_score = None, float("-inf")
        for gid, members in groups.items():
            if len(members) >= cap:
                continue
            # sum of composites to current members
            s = sum(
                pair_scores.get(frozenset({p.id, m.id}), 0.0)
                for m in members
            )
            # soft side-balance penalty per same-side member already seated
            same_side = sum(1 for m in members if m.side == p.side)
            s -= _SIDE_PENALTY * same_side
            if s > best_score:
                best_score, best_gid = s, gid
        if best_gid is None:
            # all groups at cap — pick the smallest
            best_gid = min(groups, key=lambda g: len(groups[g]))
        groups[best_gid].append(p)

    return groups


def _strip(pid: Any) -> Optional[int]:
    """'prospect-42' -> 42, else None."""
    if not isinstance(pid, str):
        return None
    try:
        return int(pid.split("-", 1)[1])
    except (ValueError, IndexError):
        return None
