"""triage/intake_extract.py : plain-English event description -> intake profile
+ rich ICP triage_config.

WHY THIS EXISTS
---------------
The intake screen (frontend SharedIntake.jsx, "Define the event") asks a host to
hand-pick chips for role / seniority / stage / format / goal, set sliders for
headcount + budget, and type a city. That is a lot of clicks for something the
host can say in one sentence:

    "Intimate dinner for seed-stage ML infra founders in SF, ~40 seats, no recruiters."

This module turns that sentence into TWO things:

  1. a NORMALIZED PROFILE that maps onto the form's fixed-vocabulary chips, so
     the screen auto-fills and the operator just reviews + tweaks; and
  2. a rich ICP ``triage_config`` (the same shape as the hand-authored
     icp_bryankim.json) carrying the details a chip CAN'T hold — anti-fit,
     nice-to-haves, and archetype priority (boost founders / cap investors) —
     compiled deterministically via ``icp_compiler.compile_icp`` so a future
     triage run scores against the host's real intent, not just the chips.

The intake screen stays mode-less: it does NOT persist anything. The rich config
rides along in client state and is persisted later, at the existing inbound
commit point (Stage02.startInbound -> setTriageConfig), so nothing about the
persistence architecture changes.

DESIGN CONTRACT
  - SNAP CHIPS TO THE FORM VOCAB. The chip fields are fixed enums; the model is
    told the EXACT allowed values and we hard-filter to them, so a hallucinated
    "Series Q" can't reach the form.
  - RICH EXTRAS ARE FREE TEXT, THEN COMPILED. anti-fit / nice-to-have are short
    phrases; archetype priority uses a small known vocab. They flow through
    compile_icp (clamps, conflict resolution, thresholds) — never raw to the
    scorer.
  - FILL-ONLY, NEVER INVENT. Omit a chip field when the description doesn't imply
    it, so the caller keeps its own default (the form merges).
  - FAIL-SOFT. No API key / malformed output -> empty profile + error, never an
    exception. The host can always fill the form by hand.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from ..jsonx import extract_json
from .icp_compiler import compile_icp

INTAKE_MODEL = os.environ.get("TRIAGE_INTAKE_MODEL", "claude-sonnet-4-6")
INTAKE_MAX_TOKENS = int(os.environ.get("TRIAGE_INTAKE_MAX_TOKENS", "1100"))

# These MUST stay in lockstep with the chip vocab in frontend/SharedIntake.jsx.
SENIORITY = ("Student", "New grad", "Junior", "Senior", "Staff+", "Leadership")
STAGES_CO = ("Pre-seed", "Seed", "Series A", "Series B+", "Enterprise")
YOE = ("0-2", "3-5", "6-10", "10+")
FORMATS = ("Sit-down dinner", "Hackathon", "Workshop", "Mixer", "Roundtable")
GOALS = ("Hiring pipeline", "Fundraising", "Sales pipeline",
         "Product testing", "Community density")
SOURCES = ("linkedin", "github", "scholar")
# Archetype vocab compile_icp / the scorer understand (see icp_agent / recommend).
ARCHETYPES = ("founder", "investor", "operator", "engineer",
              "researcher", "student", "executive")

_HEADCOUNT_MAX = 160
_BUDGET_MAX = 40000

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        from anthropic import Anthropic
        _CLIENT = Anthropic(max_retries=2)
    return _CLIENT


def _opts(label: str, values: tuple[str, ...]) -> str:
    return "%s — pick from EXACTLY: %s" % (label, ", ".join(values))


_SYSTEM = (
    "You parse a host's plain-English event description into a structured intake "
    "profile for an event-curation tool. Reply with ONLY a JSON object, no prose.\n\n"
    "Use ONLY these keys, and OMIT any key the description doesn't clearly imply "
    "(do not guess — an omitted field keeps the form's existing default):\n"
    "  role: str — the target attendee role, in the host's words "
    "(e.g. 'ML infrastructure founders').\n"
    "  " + _opts("seniority: [str]", SENIORITY) + ". Map founders/execs to "
    "'Leadership', principal/staff ICs to 'Staff+'.\n"
    "  " + _opts("co_stage: [str]", STAGES_CO) + ".\n"
    "  " + _opts("yoe: [str]", YOE) + " (years of experience bands).\n"
    "  " + _opts("format: str (single)", FORMATS) + ". Map 'fireside'/'salon'/"
    "'dinner' to 'Sit-down dinner', 'happy hour'/'meetup'/'mixer' to 'Mixer', "
    "'panel'/'talk' to 'Workshop'.\n"
    "  city: str — host city if stated.\n"
    "  event_name: str — only if the host gives an explicit name.\n"
    "  headcount: int — number of seats/guests if stated (0-%d).\n" % _HEADCOUNT_MAX +
    "  " + _opts("goal: [str]", GOALS) + " — the host's objective. 'recruiting'/"
    "'hiring' -> 'Hiring pipeline', 'raising'/'investors' -> 'Fundraising', "
    "'customers'/'sales' -> 'Sales pipeline', 'feedback'/'beta' -> 'Product "
    "testing', 'network'/'community' -> 'Community density'.\n"
    "  budget: int — total budget in USD if stated (0-%d).\n" % _BUDGET_MAX +
    "  " + _opts("sources: [str]", SOURCES) + " — discovery sources if implied "
    "('open-source'/'GitHub' -> 'github', 'papers'/'research' -> 'scholar'). "
    "'linkedin' is the default and is always fine to include.\n\n"
    "ALSO capture the curation intent a chip can't express (omit if not implied):\n"
    "  ideal_attendee_profile: str — 1-3 sentences describing who belongs in the "
    "room, in your words, richer than the chips.\n"
    "  anti_fit: [str] — concrete kinds of people who should NOT get a seat "
    "(e.g. 'recruiters prospecting for hires', 'students with no company').\n"
    "  nice_to_have: [str] — soft positive signals (e.g. 'backed by a top fund', "
    "'shipped a product with traction').\n"
    "  " + _opts("priority_archetypes: [str]", ARCHETYPES) + " — who to BOOST "
    "(most curated rooms boost 'founder').\n"
    "  " + _opts("deprioritize_archetypes: [str]", ARCHETYPES) + " — who to "
    "down-weight. An archetype must not be in both lists.\n"
    "  summary: str — ONE short sentence describing the room you parsed.\n\n"
    "Be conservative: only set a field you're confident the host meant."
)


@dataclass
class IntakeExtractResult:
    """Outcome of one extraction.

    `profile`       : normalized chip fields, safe to merge onto the form state.
    `triage_config` : rich compile_icp output (anti-fit / nice-to-have /
                      archetype_priority / thresholds) — carried downstream and
                      persisted at the inbound commit, NOT here.
    `captured`      : human-readable list of the extra (non-chip) signals we
                      pulled, so the UI can tell the host nothing was dropped.
    """
    profile: dict = field(default_factory=dict)
    triage_config: dict = field(default_factory=dict)
    summary: str = ""
    captured: list[str] = field(default_factory=list)
    error: str = ""


def _norm_enum_list(raw: object, allowed: tuple[str, ...]) -> list[str]:
    """Keep only values that match an allowed option (case-insensitive),
    de-duplicated, in the allowed order so the chips render predictably."""
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    lowered = {str(it).strip().lower() for it in items if str(it).strip()}
    return [opt for opt in allowed if opt.lower() in lowered]


def _norm_enum_one(raw: object, allowed: tuple[str, ...]) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    r = raw.strip().lower()
    for opt in allowed:
        if opt.lower() == r:
            return opt
    return None


def _norm_int(raw: object, lo: int, hi: int) -> Optional[int]:
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return None


def _norm_str_list(raw: object) -> list[str]:
    """Free-text phrase list : strip, drop blanks, de-dupe (order-preserving)."""
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        s = (it if isinstance(it, str) else str(it)).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _normalize_profile(raw: dict) -> dict:
    """The chip-vocab subset : snap each field, drop unmapped/garbage."""
    out: dict = {}
    role = raw.get("role")
    if isinstance(role, str) and role.strip():
        out["role"] = role.strip()
    city = raw.get("city")
    if isinstance(city, str) and city.strip():
        out["city"] = city.strip()
    name = raw.get("event_name")
    if isinstance(name, str) and name.strip():
        out["event_name"] = name.strip()

    for key, allowed in (("seniority", SENIORITY), ("co_stage", STAGES_CO),
                         ("yoe", YOE), ("goal", GOALS), ("sources", SOURCES)):
        vals = _norm_enum_list(raw.get(key), allowed)
        if vals:
            out[key] = vals

    fmt = _norm_enum_one(raw.get("format"), FORMATS)
    if fmt:
        out["format"] = fmt
    headcount = _norm_int(raw.get("headcount"), 0, _HEADCOUNT_MAX)
    if headcount is not None:
        out["headcount"] = headcount
    budget = _norm_int(raw.get("budget"), 0, _BUDGET_MAX)
    if budget is not None:
        out["budget"] = budget
    return out


def _build_triage_config(raw: dict, profile: dict) -> tuple[dict, list[str]]:
    """Compile the rich ICP from the extraction. Returns (triage_config,
    captured) where `captured` names the non-chip signals we pulled.

    We feed compile_icp an ICP assembled from the chip fields (as strings) plus
    the free-text extras, so the output is the same shape as icp_bryankim.json's
    nested triage_config and the scorer consumes it unchanged."""
    anti_fit = _norm_str_list(raw.get("anti_fit"))
    nice_to_have = _norm_str_list(raw.get("nice_to_have"))
    priority = _norm_enum_list(raw.get("priority_archetypes"), ARCHETYPES)
    deprioritize = _norm_enum_list(raw.get("deprioritize_archetypes"), ARCHETYPES)

    goal_list = profile.get("goal") or []
    icp = {
        "role": profile.get("role", ""),
        "seniority": ", ".join(profile.get("seniority", [])),
        "co_stage": ", ".join(profile.get("co_stage", [])),
        "format": profile.get("format", ""),
        "city": profile.get("city", ""),
        "goal": goal_list[0] if goal_list else "",
        "capacity": profile.get("headcount", 0),
        "priority_archetypes": priority,
        "deprioritize_archetypes": deprioritize,
        "anti_fit": anti_fit,
        "nice_to_have": nice_to_have,
    }
    config = compile_icp(icp)

    # Prefer the model's richer prose for the attendee profile when it gave one.
    iap = raw.get("ideal_attendee_profile")
    if isinstance(iap, str) and iap.strip():
        config["ideal_attendee_profile"] = iap.strip()

    captured: list[str] = []
    if priority:
        captured.append("priority: " + ", ".join(priority))
    if deprioritize:
        captured.append("down-weight: " + ", ".join(deprioritize))
    if anti_fit:
        captured.append("%d anti-fit signal%s" % (len(anti_fit),
                                                   "" if len(anti_fit) == 1 else "s"))
    if nice_to_have:
        captured.append("%d nice-to-have%s" % (len(nice_to_have),
                                               "" if len(nice_to_have) == 1 else "s"))
    return config, captured


def extract_intake_profile(description: str, *, client=None) -> IntakeExtractResult:
    """One-shot: NL event description -> chip profile + rich ICP triage_config.
    Never raises.

    The returned `profile` carries only the chip fields the host clearly implied
    (snapped to the form vocab); `triage_config` carries the richer curation
    intent (anti-fit / nice-to-have / archetype priority) compiled for the
    scorer. The caller merges `profile` onto its defaults and stashes
    `triage_config` for the downstream inbound commit."""
    text = (description or "").strip()
    if not text:
        return IntakeExtractResult(error="empty description")
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip() and client is None:
        return IntakeExtractResult(error="ANTHROPIC_API_KEY unset")
    try:
        cli = client or _client()
        resp = cli.messages.create(
            model=INTAKE_MODEL,
            max_tokens=INTAKE_MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        body = "".join(getattr(b, "text", "") for b in resp.content)
    except Exception as exc:  # noqa: BLE001
        return IntakeExtractResult(error=f"{type(exc).__name__}: {exc}")

    data = extract_json(body)
    if not isinstance(data, dict):
        return IntakeExtractResult(error="model did not return JSON")
    profile = _normalize_profile(data)
    triage_config, captured = _build_triage_config(data, profile)
    summary = data.get("summary")
    return IntakeExtractResult(
        profile=profile,
        triage_config=triage_config,
        captured=captured,
        summary=summary.strip() if isinstance(summary, str) else "",
    )
