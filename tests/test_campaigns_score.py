"""
Campaign fit scoring: the rules that have to hold when the enrichment pass is
wrong, not when it is right.

Three of these are load-bearing for the product rather than the arithmetic:
ideology cannot reach the scorer at all, an unsourced claim moves nothing, and
a campaign nobody can contact does not lead the queue however well it fits.
"""
from __future__ import annotations

import dataclasses

import pytest

from backend import campaigns_score as cs


def ev(url: str = "https://example.org/a", observed: str = "said a thing") -> cs.Evidence:
    return cs.Evidence(url=url, observed=observed)


def campaign(**kw) -> cs.Campaign:
    base = dict(candidate="Pat Doe", office="U.S. House", district="XX-01",
                state="Example", signals=(),
                contactability=cs.Contactability.NAMED_CONTACT)
    base.update(kw)
    return cs.Campaign(**base)


# --------------------------------------------------------------------------
# Rule 1: ideology is not an input, structurally
# --------------------------------------------------------------------------

def test_campaign_struct_carries_no_ideology_field():
    """The guarantee is the absent field, so the test is on the field list.

    If someone adds `party` to Campaign, this fails and they have to come read
    the docstring before shipping it. That is the whole point.
    """
    names = {f.name for f in dataclasses.fields(cs.Campaign)}
    for banned in ("party", "ideology", "affiliation", "positions", "lean",
                   "partisan", "endorsements"):
        assert banned not in names, f"Campaign grew a {banned!r} field"


def test_weight_table_has_no_ideological_signal():
    names = " ".join(cs.SIGNAL_WEIGHTS)
    for banned in ("party", "ideolog", "partisan", "lean", "conservative",
                   "progressive", "liberal"):
        assert banned not in names


# --------------------------------------------------------------------------
# Rule 2: an unevidenced signal scores zero
# --------------------------------------------------------------------------

def test_signal_without_evidence_scores_nothing_and_is_named():
    result = cs.score_campaign(campaign(signals=(
        cs.Signal("race_competitive", 1.0, None),
    )))
    assert result.score == 0
    assert result.unevidenced == ["race_competitive"]
    assert not result.contributions


@pytest.mark.parametrize("url,observed", [
    ("", "observed something"),          # no url
    ("not-a-url", "observed something"),  # not http
    ("https://example.org/a", ""),        # no observation
    ("https://example.org/a", "   "),     # whitespace only
])
def test_unusable_evidence_is_treated_as_absent(url, observed):
    result = cs.score_campaign(campaign(signals=(
        cs.Signal("race_competitive", 1.0, cs.Evidence(url=url, observed=observed)),
    )))
    assert result.score == 0
    assert result.unevidenced == ["race_competitive"]


def test_evidenced_signal_scores_its_weight():
    result = cs.score_campaign(campaign(signals=(
        cs.Signal("race_competitive", 1.0, ev()),
    )))
    expected = int(round(100 * cs.SIGNAL_WEIGHTS["race_competitive"] / cs.MAX_RAW))
    assert result.fit_before_contact == expected
    assert result.contributions[0].evidence is not None


def test_every_contribution_can_be_traced_to_a_url():
    """The 'why this campaign' view is only real if each point has a source."""
    result = cs.score_campaign(campaign(signals=(
        cs.Signal("race_competitive", 1.0, ev("https://a.example/1")),
        cs.Signal("campaign_scale", 0.5, ev("https://b.example/2")),
    )))
    assert all(c.evidence and c.evidence.url.startswith("http")
               for c in result.contributions)
    assert any("a.example" in line for line in result.why())


# --------------------------------------------------------------------------
# Rule 3: contactability gates rather than adds
# --------------------------------------------------------------------------

def test_perfect_fit_with_no_contact_route_scores_zero_but_keeps_its_fit():
    signals = tuple(cs.Signal(name, 1.0, ev()) for name in cs.SIGNAL_WEIGHTS)
    result = cs.score_campaign(campaign(signals=signals,
                                        contactability=cs.Contactability.NONE))
    assert result.fit_before_contact == 100
    assert result.score == 0
    assert not result.is_actionable
    assert any("no public contact" in line for line in result.why())


def test_contact_quality_scales_the_score_monotonically():
    signals = tuple(cs.Signal(name, 1.0, ev()) for name in cs.SIGNAL_WEIGHTS)
    scores = [cs.score_campaign(campaign(signals=signals, contactability=c)).score
              for c in (cs.Contactability.NONE, cs.Contactability.FORM_ONLY,
                        cs.Contactability.CAMPAIGN_GENERAL,
                        cs.Contactability.NAMED_CONTACT)]
    assert scores == sorted(scores)
    assert scores[0] == 0 and scores[-1] == 100


# --------------------------------------------------------------------------
# Arithmetic and robustness
# --------------------------------------------------------------------------

def test_weights_sum_to_one_hundred():
    assert cs.MAX_RAW == 100


def test_all_signals_at_full_strength_is_exactly_one_hundred():
    signals = tuple(cs.Signal(name, 1.0, ev()) for name in cs.SIGNAL_WEIGHTS)
    assert cs.score_campaign(campaign(signals=signals)).score == 100


def test_no_signals_is_zero_not_an_error():
    result = cs.score_campaign(campaign(signals=()))
    assert result.score == 0
    assert result.tier == "hold"


@pytest.mark.parametrize("strength,expected_fraction", [
    (-5.0, 0.0), (0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (99.0, 1.0),
])
def test_strength_is_clamped(strength, expected_fraction):
    result = cs.score_campaign(campaign(signals=(
        cs.Signal("race_competitive", strength, ev()),
    )))
    weight = cs.SIGNAL_WEIGHTS["race_competitive"]
    assert result.fit_before_contact == int(round(100 * weight * expected_fraction / cs.MAX_RAW))


def test_duplicate_signal_does_not_double_count():
    one = cs.score_campaign(campaign(signals=(
        cs.Signal("race_competitive", 1.0, ev()),
    )))
    twice = cs.score_campaign(campaign(signals=(
        cs.Signal("race_competitive", 1.0, ev()),
        cs.Signal("race_competitive", 1.0, ev("https://other.example/x")),
    )))
    assert one.score == twice.score


def test_unknown_signal_is_reported_not_silently_dropped():
    result = cs.score_campaign(campaign(signals=(
        cs.Signal("race_compettive", 1.0, ev()),      # typo
    )))
    assert result.unknown_signals == ["race_compettive"]
    assert result.score == 0


@pytest.mark.parametrize("score,tier", [
    (100, "high"), (75, "high"), (74, "medium"), (50, "medium"),
    (49, "low"), (25, "low"), (24, "hold"), (0, "hold"),
])
def test_tier_bands(score, tier):
    assert cs._tier_for(score) == tier


def test_scoring_is_pure():
    """Same input twice, same answer -- no clock, no network, no accumulation."""
    c = campaign(signals=(cs.Signal("campaign_scale", 0.7, ev()),))
    assert cs.score_campaign(c) == cs.score_campaign(c)


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

def test_rank_orders_by_score_then_breaks_ties_stably():
    strong = campaign(candidate="Zoe", signals=(
        cs.Signal("race_competitive", 1.0, ev()),
        cs.Signal("campaign_scale", 1.0, ev()),
    ))
    weak_a = campaign(candidate="Ana", signals=(cs.Signal("civic_tech_interest", 0.2, ev()),))
    weak_b = campaign(candidate="Bob", signals=(cs.Signal("civic_tech_interest", 0.2, ev()),))

    ordered = [c.candidate for c, _ in cs.rank([weak_b, strong, weak_a])]
    assert ordered[0] == "Zoe"
    assert ordered[1:] == ["Ana", "Bob"]        # tie broken by name, stably
    assert ordered == [c.candidate for c, _ in cs.rank([weak_a, weak_b, strong])]


def test_rank_puts_reachable_ahead_of_equally_good_unreachable():
    signals = (cs.Signal("race_competitive", 1.0, ev()),)
    reachable = campaign(candidate="Reachable", signals=signals,
                         contactability=cs.Contactability.NAMED_CONTACT)
    unreachable = campaign(candidate="Aaa Unreachable", signals=signals,
                           contactability=cs.Contactability.NONE)
    ordered = [c.candidate for c, _ in cs.rank([unreachable, reachable])]
    assert ordered[0] == "Reachable"
