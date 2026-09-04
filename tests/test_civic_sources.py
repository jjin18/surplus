"""
Civic retrieval: which rung a source lands on, and what we send the model.

The classification rules here are the ones that hold when the model is wrong,
so they are tested against the cases where a source claims more than it earns.
"""
from __future__ import annotations

import pytest

from backend import civic_sources as cs


# --------------------------------------------------------------------------
# Hosts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.census.gov/data", "census.gov"),
    ("http://News.Example.com/a/b", "news.example.com"),
    ("https://sub.domain.co.uk:8443/x", "sub.domain.co.uk"),
    ("ftp://files.example.com", ""),
    ("not a url", ""),
    ("", ""),
])
def test_host_of(url, expected):
    assert cs.host_of(url) == expected


# --------------------------------------------------------------------------
# Classification : a source cannot claim a stronger rung than it earns
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,claimed,expected", [
    # Publishers that settle it outright.
    ("https://www.census.gov/data/rent", "E", "A"),
    ("https://data.cityofoakland.gov/x", "E", "A"),
    ("https://www.legislation.gov.uk/x", "F", "A"),
    ("https://www.aeaweb.org/articles/x", "E", "B"),
    ("https://arxiv.org/abs/2401.001", "C", "B"),
    ("https://www.reddit.com/r/oakland/x", "A", "F"),
    ("https://x.com/someone/status/1", "B", "F"),
    ("https://medium.com/@someone/post", "C", "F"),
    # Universities and institutes: capped at C, trusted below it.
    ("https://www.brookings.edu/articles/x", "A", "C"),
    ("https://terner.berkeley.edu/report", "B", "C"),
    ("https://news.stanford.edu/story", "E", "E"),
    ("https://www.rand.org/pubs/x", "D", "D"),
    # Everything else keeps what it claimed.
    ("https://www.sfchronicle.com/story", "E", "E"),
    ("https://www.sfchronicle.com/story", "D", "D"),
    ("https://www.sfchronicle.com/story", "nonsense", "E"),
])
def test_classify(url, claimed, expected):
    assert cs.classify(url, claimed) == expected


def test_is_social_covers_subdomains():
    assert cs.is_social("https://old.reddit.com/r/oakland")
    assert cs.is_social("https://twitter.com/x/status/1")
    assert not cs.is_social("https://www.census.gov/data")


# --------------------------------------------------------------------------
# gather()
# --------------------------------------------------------------------------

def _fake_search(results_by_tier):
    """Stand in for one Exa call per rung, without the network."""
    calls = []

    def search(step, question, place, harder):
        calls.append({"tier": step["tier"], "question": question,
                      "place": place, "harder": harder})
        return [
            {"tier": cs.classify(url, step["tier"]), "found_by": step["tier"],
             "title": f"{step['tier']} result", "url": url, "host": cs.host_of(url),
             "published": "2026-01-02", "snippet": "some text"}
            for url in results_by_tier.get(step["tier"], [])
        ]

    return search, calls


def test_gather_searches_every_rung_once_and_orders_the_results():
    search, calls = _fake_search({
        "E": ["https://news.example.com/story"],
        "A": ["https://www.census.gov/data"],
        "F": ["https://www.reddit.com/r/x/1"],
    })
    results = cs.gather("Why is rent up?", "Oakland, CA", search=search)

    assert sorted(c["tier"] for c in calls) == ["A", "B", "C", "D", "E", "F"]
    assert [r["tier"] for r in results] == ["A", "E", "F"]
    assert calls[0]["place"] == "Oakland, CA"
    assert all(c["harder"] is False for c in calls)


def test_gather_drops_a_url_found_by_two_rungs():
    same = "https://www.census.gov/data"
    search, _ = _fake_search({"A": [same], "C": [same], "E": [same]})
    results = cs.gather("Why?", "", search=search)
    assert [r["url"] for r in results] == [same]


def test_gather_passes_the_harder_flag_through():
    search, calls = _fake_search({})
    cs.gather("Why?", "", harder=True, search=search)
    assert all(c["harder"] is True for c in calls)


def test_gather_needs_a_question():
    search, calls = _fake_search({"A": ["https://www.census.gov/data"]})
    assert cs.gather("   ", "Oakland", search=search) == []
    assert calls == []


# --------------------------------------------------------------------------
# What the model reads
# --------------------------------------------------------------------------

def test_prompt_block_lists_tier_host_and_url():
    block = cs.as_prompt_block([{
        "tier": "A", "found_by": "A", "title": "Rent data", "host": "census.gov",
        "url": "https://www.census.gov/data", "published": "2026-01-02",
        "snippet": "Median rent rose 15%.",
    }])
    assert "tier A" in block
    assert "https://www.census.gov/data" in block
    assert "Median rent rose 15%." in block


def test_prompt_block_says_so_when_nothing_was_found():
    block = cs.as_prompt_block([])
    assert "not answer from memory" in block


def test_available_follows_the_shared_exa_key(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "  key-with-spaces  ")
    assert cs.available()
    monkeypatch.setenv("EXA_API_KEY", "")
    assert not cs.available()
