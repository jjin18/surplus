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
    results = cs.gather("Why is rent up?", "Oakland, CA", search=search, backends={})

    assert sorted(c["tier"] for c in calls) == ["A", "B", "C", "D", "E", "F"]
    assert [r["tier"] for r in results] == ["A", "E", "F"]
    assert calls[0]["place"] == "Oakland, CA"
    assert all(c["harder"] is False for c in calls)


def test_gather_drops_a_url_found_by_two_rungs():
    same = "https://www.census.gov/data"
    search, _ = _fake_search({"A": [same], "C": [same], "E": [same]})
    results = cs.gather("Why?", "", search=search, backends={})
    assert [r["url"] for r in results] == [same]


def test_gather_passes_the_harder_flag_through():
    search, calls = _fake_search({})
    cs.gather("Why?", "", harder=True, search=search, backends={})
    assert all(c["harder"] is True for c in calls)


def test_gather_needs_a_question():
    search, calls = _fake_search({"A": ["https://www.census.gov/data"]})
    assert cs.gather("   ", "Oakland", search=search, backends={}) == []
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


# --------------------------------------------------------------------------
# The backends : parse what each API actually returns, and never raise
# --------------------------------------------------------------------------

def _json(monkeypatch, payload):
    """Stand in for one keyless API call."""
    calls = []

    def fake(url, params, timeout=None):
        calls.append({"url": url, "params": params})
        return payload

    monkeypatch.setattr(cs, "_get_json", fake)
    return calls


def test_openalex_rebuilds_the_inverted_abstract(monkeypatch):
    _json(monkeypatch, {"results": [{
        "display_name": "Supply and rents",
        "publication_date": "2024-03-01",
        "primary_location": {"landing_page_url": "https://www.aeaweb.org/articles/x",
                             "source": {"display_name": "AEJ"}},
        # OpenAlex ships abstracts as word -> positions.
        "abstract_inverted_index": {"New": [0], "supply": [1], "lowers": [2], "rents": [3]},
    }]})
    [item] = cs._openalex("rent", "")
    assert item["tier"] == "B"
    assert item["url"] == "https://www.aeaweb.org/articles/x"
    assert item["snippet"] == "New supply lowers rents"
    assert item["published"] == "2024-03-01"


def test_crossref_reads_the_first_title_and_strips_markup(monkeypatch):
    _json(monkeypatch, {"message": {"items": [{
        "title": ["Rent control and supply"],
        "URL": "https://doi.org/10.1234/abc",
        "abstract": "<jats:p>We find a 2% effect.</jats:p>",
        "container-title": ["Journal of Urban Economics"],
        "issued": {"date-parts": [[2023, 7, 4]]},
    }]}})
    [item] = cs._crossref("rent control", "")
    assert item["tier"] == "B"
    assert item["snippet"] == "We find a 2% effect."
    assert item["published"] == "2023-07-04"


def test_federal_register_keeps_the_agency_when_there_is_no_abstract(monkeypatch):
    _json(monkeypatch, {"results": [{
        "title": "Housing Choice Voucher Program",
        "html_url": "https://www.federalregister.gov/documents/2026/01/02/x",
        "publication_date": "2026-01-02",
        "abstract": "",
        "agencies": [{"name": "Housing and Urban Development Department"}],
    }]})
    [item] = cs._federal_register("vouchers", "Oakland")
    assert item["tier"] == "A"          # .gov settles it
    assert "Housing and Urban Development" in item["snippet"]


def test_data_gov_builds_the_dataset_url_from_the_slug(monkeypatch):
    _json(monkeypatch, {"result": {"results": [{
        "title": "Median rent by county",
        "name": "median-rent-by-county",
        "notes": "Annual series.",
        "organization": {"title": "Census Bureau"},
    }]}})
    [item] = cs._data_gov("rent", "California")
    assert item["url"] == "https://catalog.data.gov/dataset/median-rent-by-county"
    assert item["tier"] == "A"


def test_govtrack_names_the_bill_and_its_status(monkeypatch):
    _json(monkeypatch, {"objects": [{
        "display_number": "H.R. 1234",
        "title": "H.R. 1234: Housing Supply Act",
        "title_without_number": "Housing Supply Act",
        "link": "https://www.govtrack.us/congress/bills/119/hr1234",
        "current_status": "referred_to_committee",
        "current_status_date": "2026-02-11",
    }]})
    [item] = cs._govtrack("housing supply", "")
    assert item["tier"] == "A"
    assert item["title"].startswith("H.R. 1234")
    assert "referred to committee" in item["snippet"]


def test_uk_parliament_builds_the_bill_url(monkeypatch):
    _json(monkeypatch, {"items": [{
        "billId": 3712,
        "shortTitle": "Renters' Rights Bill",
        "currentHouse": "Lords",
        "currentStage": {"description": "Committee stage"},
        "lastUpdate": "2026-05-06T10:00:00",
    }]})
    [item] = cs._uk_parliament("renters rights", "")
    assert item["url"] == "https://bills.parliament.uk/bills/3712"
    assert item["published"] == "2026-05-06"
    assert "Committee stage" in item["snippet"]


def test_gdelt_carries_the_source_country(monkeypatch):
    _json(monkeypatch, {"articles": [{
        "title": "Council debates rent cap",
        "url": "https://www.thehindu.com/news/cities/kochi/x",
        "domain": "thehindu.com",
        "sourcecountry": "India",
        "seendate": "20260301T120000Z",
    }]})
    [item] = cs._gdelt("rent cap", "Kochi")
    assert item["tier"] == "E"
    assert "India" in item["snippet"]


def test_hacker_news_falls_back_to_the_discussion_url(monkeypatch):
    _json(monkeypatch, {"hits": [{"title": "Rent control study",
                                  "url": None, "objectID": "4242",
                                  "created_at": "2026-01-05T00:00:00Z"}]})
    [item] = cs._hacker_news("rent control", "")
    assert item["url"] == "https://news.ycombinator.com/item?id=4242"
    assert item["tier"] == "F"


def test_reddit_builds_the_permalink_and_stays_tier_f(monkeypatch):
    _json(monkeypatch, {"data": {"children": [{"data": {
        "title": "Rents up 20% here",
        "permalink": "/r/oakland/comments/abc/rents_up/",
        "selftext": "Anyone else seeing this?",
        "subreddit": "oakland",
    }}]}})
    [item] = cs._reddit("rent", "Oakland")
    assert item["url"] == "https://www.reddit.com/r/oakland/comments/abc/rents_up/"
    assert item["tier"] == "F"


def test_a_backend_that_returns_something_unexpected_yields_nothing(monkeypatch):
    _json(monkeypatch, {"unexpected": "shape"})
    for fn in (cs._openalex, cs._crossref, cs._federal_register, cs._data_gov,
               cs._govtrack, cs._uk_parliament, cs._gdelt, cs._hacker_news, cs._reddit):
        assert fn("rent", "Oakland") == []


def test_a_result_without_a_url_is_dropped():
    assert cs._result("A", "test", "No link", "") is None
    assert cs._result("A", "test", "Not a url", "not-a-url") is None


@pytest.mark.parametrize("place,expected", [
    ("Oakland, California, US", "California"),
    ("Brooklyn, New York, US", "New York"),
    ("Kochi, Kerala, IN", ""),
    ("", ""),
])
def test_state_of_reads_the_us_state_for_the_legislature_query(place, expected):
    assert cs._state_of(place) == expected


def test_one_backend_failing_does_not_lose_the_others(monkeypatch):
    def good(question, place): return [cs._result("E", "good", "t",
                                                  "https://news.example.com/a")]

    def bad(question, place): raise RuntimeError("HTTP 429: slow down")

    results = cs.gather("Why?", "Oakland", backends={
        "good": (good, False), "bad": (bad, False),
    })
    assert [r["url"] for r in results] == ["https://news.example.com/a"]
    assert cs.LAST_RUN["good"] == 1
    assert "429" in cs.LAST_RUN["bad"]


def test_the_probe_reports_every_backend(monkeypatch):
    _json(monkeypatch, {"results": [], "message": {"items": []}, "items": [],
                        "objects": [], "articles": [], "hits": [],
                        "data": {"children": []}, "result": {"results": []}})
    report = cs.probe("housing", "California")
    assert set(report["backends"]) >= {"openalex", "crossref", "gdelt", "govtrack"}
    assert report["backends"]["openalex"]["results"] == 0


# --------------------------------------------------------------------------
# Free APIs under load : a 429 is an event, not a fault
# --------------------------------------------------------------------------

def test_google_news_reads_the_rss_feed(monkeypatch):
    feed = """<rss><channel>
      <item><title>Council debates rent cap</title>
            <link>https://www.mercurynews.com/story</link>
            <pubDate>Tue, 03 Mar 2026 08:00:00 GMT</pubDate>
            <source url="https://www.mercurynews.com">Mercury News</source></item>
      <item><title><![CDATA[Housing plan advances]]></title>
            <link>https://www.sfchronicle.com/story2</link>
            <source url="https://www.sfchronicle.com">SF Chronicle</source></item>
    </channel></rss>"""
    monkeypatch.setattr(cs, "_get_text", lambda url, params, timeout=None: feed)
    items = cs._google_news("rent cap", "Oakland")
    assert [i["url"] for i in items] == ["https://www.mercurynews.com/story",
                                         "https://www.sfchronicle.com/story2"]
    assert items[0]["tier"] == "E"
    assert items[0]["snippet"] == "Mercury News"
    assert items[1]["title"] == "Housing plan advances"      # CDATA unwrapped


def test_a_rate_limited_backend_is_reported_as_such_not_as_an_error():
    def limited(question, place): raise cs.RateLimited("rate_limited (HTTP 429)")

    def fine(question, place): return [cs._result("E", "fine", "t",
                                                  "https://news.example.com/a")]

    cs._RESULT_CACHE.clear()
    results = cs.gather("Why?", "Oakland", backends={
        "gdelt": (limited, True), "google_news": (fine, True)})
    assert cs.LAST_RUN["gdelt"] == "rate_limited"
    assert cs.LAST_RUN["google_news"] == 1
    assert len(results) == 1          # the ladder is built from what answered


def test_a_backend_answer_is_reused_for_a_few_minutes(monkeypatch):
    cs._RESULT_CACHE.clear()
    calls = []

    def counted(question, place):
        calls.append(1)
        return [cs._result("E", "counted", "t", "https://news.example.com/a")]

    for _ in range(3):
        cs.gather("Why is rent up?", "Oakland", backends={"news": (counted, True)})
    assert len(calls) == 1            # asked once, served three times
    cs.gather("A different question?", "Oakland", backends={"news": (counted, True)})
    assert len(calls) == 2            # a different question is a different call
    cs._RESULT_CACHE.clear()


def test_one_retry_then_rate_limited(monkeypatch):
    attempts = []

    class _Resp:
        status_code = 429
        text = "slow down"

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            attempts.append(1)
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    with pytest.raises(cs.RateLimited):
        cs._fetch("https://api.gdeltproject.org/x", {})
    assert len(attempts) == 2         # tried, backed off, tried once more


# --------------------------------------------------------------------------
# What the first live probe found
# --------------------------------------------------------------------------

def test_data_gov_falls_back_to_the_legacy_path_when_the_gateway_404s(monkeypatch):
    tried = []

    def fake(url, params, timeout=None):
        tried.append(url)
        if "api/action" in url:
            return {"result": {"results": [{"title": "Rents", "name": "rents",
                                            "notes": "Series."}]}}
        raise RuntimeError("HTTP 404: gateway")

    monkeypatch.setattr(cs, "_get_json", fake)
    [item] = cs._data_gov("rent", "California")
    assert item["url"].endswith("/dataset/rents")


def test_data_gov_reports_down_when_neither_path_answers(monkeypatch):
    monkeypatch.setattr(cs, "_get_json",
                        lambda url, params, timeout=None: (_ for _ in ()).throw(
                            RuntimeError("HTTP 404")))
    with pytest.raises(RuntimeError):
        cs._data_gov("rent", "California")


def test_hacker_news_cites_the_thread_not_the_newspaper_it_links_to(monkeypatch):
    _json(monkeypatch, {"hits": [{
        "title": "Rents are up in Oakland",
        "url": "https://www.mercurynews.com/business/story",   # journalism, tier E
        "objectID": "4242", "points": 120, "num_comments": 87,
        "created_at": "2026-01-05T00:00:00Z"}]})
    [item] = cs._hacker_news("rent", "")
    # Citing the article from here would put reporting on the bottom rung and
    # dress the thread up as its source.
    assert item["url"] == "https://news.ycombinator.com/item?id=4242"
    assert item["tier"] == "F"
    assert "mercurynews.com" in item["snippet"]


def test_a_403_is_recorded_as_blocked_not_as_an_error():
    def blocked(question, place): raise cs.Blocked("blocked (HTTP 403)")

    cs._RESULT_CACHE.clear()
    cs.gather("Why?", "Oakland", backends={"reddit": (blocked, True)})
    assert cs.LAST_RUN["reddit"] == "blocked"


def test_exa_pauses_itself_when_it_is_out_of_credits(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "k")
    monkeypatch.setattr(cs, "_exa_paused_until", 0.0)

    class _Resp:
        status_code = 402
        text = '{"error":"You have exceeded your credits limit."}'

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    assert cs.available()
    with pytest.raises(RuntimeError):
        cs._post({"query": "x"})
    # Six parallel 402s per question buys nothing ; stop asking for a while.
    assert not cs.available()
    assert "402" in cs.exa_status()
    monkeypatch.setattr(cs, "_exa_paused_until", 0.0)


# --------------------------------------------------------------------------
# Text off someone else's server: linear scans, never a backtracking regex
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("<jats:p>We find a 2% effect.</jats:p>", "We find a 2% effect."),
    ("<p>Nested <b>markup</b> here</p>", "Nested markup here"),
    ("<!-- a comment --> visible", "visible"),
    ('<a href="a>b">link</a> text', "link text"),      # quoted > in an attribute
    ("Rent &amp; supply &lt;2%&gt;", "Rent & supply <2%>"),
    ("", ""),
])
def test_clean_strips_markup_without_a_tag_regex(raw, expected):
    assert cs._clean(raw) == expected


def test_clean_bounds_the_work_before_doing_any():
    # A hostile "feed" cannot buy more than a bounded scan.
    assert len(cs._clean("<b>" * 50_000 + "x")) <= cs.SNIPPET_CHARS


def test_items_scans_the_feed_and_stops():
    feed = "<rss>" + "<item><title>t</title></item>" * 30 + "</rss>"
    blocks = cs._items(feed)
    assert len(blocks) == 12                    # capped, not unbounded
    assert cs._tag(blocks[0], "title") == "t"


def test_items_survives_an_unclosed_item():
    assert cs._items("<item><title>t</title>") == []
    assert cs._tag("<title>unterminated", "title") == ""


def test_tag_reads_attributes_and_cdata():
    block = '<source url="https://x.com">Mercury News</source>'
    assert cs._tag(block, "source") == "Mercury News"
    assert cs._tag("<title><![CDATA[Housing plan]]></title>", "title") == "Housing plan"


# --------------------------------------------------------------------------
# An ordinance is primary law, not somebody's website
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://library.municode.com/ca/oakland/codes/planning_code",
    "https://codelibrary.amlegal.com/codes/sanfrancisco/latest/sf_planning",
    "https://ecode360.com/12345678",
    "https://www.legislation.gov.uk/ukpga/2024/1",
])
def test_a_municipal_code_page_is_official_data(url):
    # The code section itself outranks any reporting about it, so it belongs
    # on the top rung even when the model claimed something weaker.
    assert cs.classify(url, "E") == "A"


def test_the_code_hosts_have_their_own_rung_in_the_exa_plan():
    code_step = [s for s in cs.TIER_PLAN if "municipal code" in s["query"]]
    assert len(code_step) == 1
    assert code_step[0]["tier"] == "A"
    assert "library.municode.com" in code_step[0]["domains"]
