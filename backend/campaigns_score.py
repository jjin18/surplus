"""campaigns_score.py : campaign fit scoring — deterministic, evidenced, and
structurally incapable of scoring on ideology.

WHAT THIS IS FOR. Given a 2026 campaign, answer one question: should this be
near the top of the contact list? Not "is this campaign good", not "do we agree
with them" -- only "does this campaign have a problem we can solve, and can we
actually reach them". The output is a number plus the reasoning that produced
it, because a rank nobody can interrogate is a rank nobody should act on.

TWO DESIGN RULES CARRY MOST OF THE WEIGHT HERE.

1. IDEOLOGY IS NOT AN INPUT -- BY CONSTRUCTION, NOT BY CONVENTION.
   `Campaign` below has no party field, no ideology field, no candidate
   positions. Not "we agree not to look at party": the scorer cannot read party
   because the struct it is handed does not carry it. A comment saying "don't
   score on ideology" is a promise; a dataclass without the field is a
   guarantee, and it is the one that still holds after six months of edits by
   someone who never read this docstring. Selling infrastructure to both sides
   is a positioning claim that has to survive someone reading the code, so the
   code is where it is enforced.

   If party is ever needed for a legitimate reason (deduping two candidates of
   the same name in one district, say), put it on a separate record that the
   scorer is not handed. Do not add it here.

2. AN UNEVIDENCED SIGNAL SCORES ZERO.
   Every signal must arrive with an `Evidence` -- a URL and what was observed
   there. A signal asserted without a source contributes nothing and is
   reported in `unevidenced`. This is the same rule civic.py applies to
   answers ("an answer with an empty ladder is refused, not rendered"), applied
   to ranking: the failure mode of an LLM enrichment pass is confident
   invention, and the cheapest defence is refusing to let unsourced claims move
   a number. It also means every point in the final score can be traced to a
   page you can open, which is what makes the "why this campaign?" view real
   rather than a plausible-sounding summary.

CONTACTABILITY IS A GATE, NOT A SIGNAL. A campaign with no reachable public
contact cannot be contacted first no matter how well it fits, so reachability
multiplies the score rather than adding to it. The pre-gate score is kept as
`fit_before_contact` -- a strong fit that is merely unreachable is a research
task, not a dead lead, and collapsing both to "low score" would hide that.

DELIBERATELY NOT HERE: send decisions, templates, anything that touches an
outbound channel. This module is pure policy -- no DB, no network, no clock --
in the same spirit as config.py and redaction.py. It ranks; something else
decides what to do about the ranking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# --- the levers -------------------------------------------------------------
# Weights sum to 100 before the contactability gate. These are policy: change
# them to change what "good fit" means, and the explanation re-prices itself
# because every reason is generated from the same table.
SIGNAL_WEIGHTS: dict[str, int] = {
    # A close race spends more, moves faster, and feels operational pain
    # earliest. The single strongest predictor that anyone will take the call.
    "race_competitive": 22,
    # Staff and budget: enough operation to have a workflow problem worth
    # paying to fix, rather than three people and a spreadsheet.
    "campaign_scale": 16,
    # They already do outreach at volume, so the problem we solve is one they
    # are having this week rather than one we have to convince them of.
    "stated_pain_point": 14,
    # An active, maintained digital operation -- the campaign is reachable
    # where software lives, and someone there owns tooling.
    "digital_presence": 14,
    # Demonstrated willingness to adopt tools. Not "uses a website": uses
    # something that required a procurement decision.
    "technology_signal": 12,
    # Volunteers are the coordination load that most campaign software is
    # actually bought to carry.
    "volunteer_operation": 12,
    # Explicit interest in civic tech / AI. Rare, and worth a lot when present
    # because it removes the whole "why would a campaign use this" conversation.
    "civic_tech_interest": 10,
}

MAX_RAW = sum(SIGNAL_WEIGHTS.values())   # 100

# How much of the fit survives when you cannot actually reach anyone. A
# campaign you cannot contact is not a lead this week; it is a research task.
CONTACT_MULTIPLIER: dict[str, float] = {
    "named_contact": 1.00,    # a named staffer with a public business address
    "campaign_general": 0.80, # a real campaign inbox, no named human yet
    "form_only": 0.45,        # a website contact form and nothing else
    "none": 0.00,             # no public route in : unactionable as a lead
}

# Bands for the list view. Cutoffs are inclusive lower bounds.
TIER_CUTOFFS: list[tuple[int, str]] = [
    (75, "high"),
    (50, "medium"),
    (25, "low"),
    (0, "hold"),
]


class Contactability(str, Enum):
    NAMED_CONTACT = "named_contact"
    CAMPAIGN_GENERAL = "campaign_general"
    FORM_ONLY = "form_only"
    NONE = "none"


@dataclass(frozen=True)
class Evidence:
    """Where a signal came from. `observed` is what the page actually said --
    a quote or a close paraphrase, not the conclusion drawn from it, so a
    reviewer can check the inference rather than just the citation."""
    url: str
    observed: str
    retrieved: str = ""       # ISO date, when known

    def is_usable(self) -> bool:
        return bool((self.url or "").startswith("http") and (self.observed or "").strip())


@dataclass(frozen=True)
class Signal:
    """One scored observation. `strength` is 0.0-1.0 : how strongly the
    evidence supports the signal, not how important the signal is (importance
    lives in SIGNAL_WEIGHTS, so the two can be tuned independently)."""
    name: str
    strength: float
    evidence: Optional[Evidence] = None

    def clamped(self) -> float:
        return max(0.0, min(1.0, float(self.strength or 0.0)))


@dataclass(frozen=True)
class Campaign:
    """A campaign as the scorer is allowed to see it.

    Note what is absent: party, ideology, candidate positions, incumbency of a
    given party. See rule 1 in the module docstring -- the omission is the
    enforcement mechanism and removing it defeats the design.
    """
    candidate: str
    office: str                       # "U.S. House", "U.S. Senate", ...
    district: str = ""                # "CA-12", "" for statewide
    state: str = ""
    signals: tuple[Signal, ...] = ()
    contactability: Contactability = Contactability.NONE


@dataclass
class Contribution:
    signal: str
    weight: int
    strength: float
    points: float
    evidence: Optional[Evidence]

    @property
    def reason(self) -> str:
        where = f" ({self.evidence.url})" if self.evidence else ""
        return (f"{self.signal.replace('_', ' ')}: "
                f"{self.points:.0f}/{self.weight} pts{where}")


@dataclass
class FitResult:
    """The score and everything needed to defend it."""
    score: int                        # 0-100, after the contactability gate
    fit_before_contact: int           # 0-100, before it
    tier: str
    contactability: Contactability
    contributions: list[Contribution] = field(default_factory=list)
    unevidenced: list[str] = field(default_factory=list)
    unknown_signals: list[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        """Can we act on this now? A strong fit behind a `none` contactability
        is real but not yet a lead -- surface it for research, not for send."""
        return self.contactability is not Contactability.NONE

    def why(self) -> list[str]:
        """The 'why this campaign?' lines, strongest contribution first."""
        lines = [c.reason for c in
                 sorted(self.contributions, key=lambda c: c.points, reverse=True)
                 if c.points > 0]
        if not self.is_actionable:
            lines.append("no public contact route found : research before contacting")
        if self.unevidenced:
            lines.append("ignored for lack of a source: " + ", ".join(sorted(self.unevidenced)))
        return lines


def _tier_for(score: int) -> str:
    for cutoff, name in TIER_CUTOFFS:
        if score >= cutoff:
            return name
    return "hold"


def score_campaign(campaign: Campaign) -> FitResult:
    """Score one campaign. Pure : same input, same output, no clock, no network.

    Signals with no usable evidence contribute zero and are named in
    `unevidenced`. Signals the weight table does not know are ignored and named
    in `unknown_signals` -- a typo in an enrichment pass should show up as a
    visible nothing rather than a silent nothing.
    """
    contributions: list[Contribution] = []
    unevidenced: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()

    for signal in campaign.signals:
        weight = SIGNAL_WEIGHTS.get(signal.name)
        if weight is None:
            unknown.append(signal.name)
            continue
        if signal.name in seen:
            # Two claims for one signal : keep the first, ignore the rest,
            # rather than letting a duplicated enrichment double-count.
            continue
        seen.add(signal.name)

        if not (signal.evidence and signal.evidence.is_usable()):
            unevidenced.append(signal.name)
            continue

        strength = signal.clamped()
        contributions.append(Contribution(
            signal=signal.name,
            weight=weight,
            strength=strength,
            points=weight * strength,
            evidence=signal.evidence,
        ))

    raw = sum(c.points for c in contributions)
    fit_before_contact = int(round(100 * raw / MAX_RAW)) if MAX_RAW else 0
    multiplier = CONTACT_MULTIPLIER.get(campaign.contactability.value, 0.0)
    score = int(round(fit_before_contact * multiplier))

    return FitResult(
        score=score,
        fit_before_contact=fit_before_contact,
        tier=_tier_for(score),
        contactability=campaign.contactability,
        contributions=contributions,
        unevidenced=unevidenced,
        unknown_signals=unknown,
    )


def rank(campaigns: list[Campaign]) -> list[tuple[Campaign, FitResult]]:
    """Score a list and order it for the contact queue.

    Ties break on `fit_before_contact` then candidate name, so the order is
    stable across runs -- a queue that reshuffles between page loads is one
    nobody can work through systematically.
    """
    scored = [(c, score_campaign(c)) for c in campaigns]
    scored.sort(key=lambda pair: (-pair[1].score,
                                  -pair[1].fit_before_contact,
                                  pair[0].candidate.lower()))
    return scored
