"""
Civic policy search: the rules that make the answer trustworthy.

The evidence hierarchy is a product rule, not a prompt suggestion, so the
tests that matter here are the ones that hold when the model misbehaves: an
invented URL is dropped, a thin answer is searched again, prose instead of
JSON fails loudly with the raw text kept for the UI.
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from backend import civic, civic_sources
from backend.main import app


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------

GOOD_URL = "https://www.census.gov/data/rent"               # a government host: tier A
STUDY_URL = "https://www.aeaweb.org/articles/rent-supply"   # a journal: tier B by host
NEWS_URL = "https://news.example.com/story"                 # no signal in the host: tier as claimed
INVENTED = "https://totally-made-up.example.org/never-searched"


def _search_block(*urls):
    return {"type": "web_search_tool_result",
            "content": [{"type": "web_search_result", "url": u} for u in urls]}


def _text_block(payload):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return {"type": "text", "text": body}


class _Response:
    def __init__(self, content):
        self.content = content


def _answer_payload(**over):
    payload = {
        "headline": "Rents rose because supply stalled while demand kept climbing.",
        "summary": "Median asking rent is up 15% since 2023.",
        "mechanisms": [
            {"mechanism": "Permits fell", "confidence": "high", "because": "Filings are down 40%."},
            {"mechanism": "In-migration", "confidence": "medium", "because": "Net inflow doubled."},
            {"mechanism": "Rate lock-in", "confidence": "low", "because": "Owners are not selling."},
        ],
        "evidence": [
            {"tier": "A", "claim": "Median rent +15%", "source": "Census", "url": GOOD_URL},
            {"tier": "B", "claim": "Supply cuts rent 2%", "source": "Study", "url": STUDY_URL},
            {"tier": "E", "claim": "Council debated the cap", "source": "Local paper", "url": NEWS_URL},
        ],
        "disputed": ["Whether the cap lowers rents."],
        "live_decisions": ["Council vote on the zoning overlay, Oct 2."],
        "actions": [
            {"effort": 1, "what": "Email your council member", "why": "Comment is counted", "url": ""},
            {"effort": 2, "what": "Read the staff report", "why": "It names the tradeoff", "url": NEWS_URL},
        ],
        "caveat": "None of this says what your own landlord will do.",
    }
    payload.update(over)
    return payload


@pytest.fixture(autouse=True)
def _clear_cache():
    civic.cache_clear()
    yield
    civic.cache_clear()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def test_extract_json_strips_fences_and_prose():
    text = 'Sure, here you go:\n```json\n{"headline": "hi"}\n```\nHope that helps.'
    assert civic.extract_json(text) == {"headline": "hi"}


def test_extract_json_keeps_raw_text_for_the_show_what_we_found_toggle():
    with pytest.raises(civic.CivicError) as exc:
        civic.extract_json("I could not find anything about that.")
    assert exc.value.code == "not_json"
    assert "could not find anything" in exc.value.raw


def test_extract_json_rejects_a_json_array():
    with pytest.raises(civic.CivicError):
        civic.extract_json("[1, 2, 3]")


def test_search_urls_reads_results_and_citations():
    content = [
        _search_block(GOOD_URL),
        {"type": "text", "text": "x", "citations": [{"url": "http://WWW.News.Example.com/story/"}]},
    ]
    urls = civic.search_urls(content)
    assert civic.normalise_url(GOOD_URL) in urls
    # Host case, www and a trailing slash are noise, not a different source.
    assert civic.normalise_url(NEWS_URL) in urls


def test_normalise_url_rejects_non_web_urls():
    assert civic.normalise_url("javascript:alert(1)") == ""
    assert civic.normalise_url("") == ""
    assert civic.normalise_url(None) == ""


# --------------------------------------------------------------------------
# Validation : the hierarchy, enforced in code
# --------------------------------------------------------------------------

def _allowed(*urls):
    return {civic.normalise_url(u) for u in urls}


def test_validate_drops_evidence_whose_url_was_never_searched():
    payload = _answer_payload(evidence=[
        {"tier": "A", "claim": "Real", "source": "Census", "url": GOOD_URL},
        {"tier": "A", "claim": "Invented", "source": "Census", "url": INVENTED},
        {"tier": "B", "claim": "No link at all", "source": "Someone", "url": ""},
    ])
    answer, notes = civic.validate(payload, _allowed(GOOD_URL))
    assert [e["claim"] for e in answer["evidence"]] == ["Real"]
    assert notes["evidence_dropped"] == 2


def test_validate_puts_a_nonsense_tier_where_the_publisher_says():
    payload = _answer_payload(evidence=[
        {"tier": "G", "claim": "Off the ladder", "source": "Census", "url": GOOD_URL},
    ])
    answer, notes = civic.validate(payload, _allowed(GOOD_URL))
    assert answer["tiers"] == ["A"]          # census.gov is official data
    assert notes["tiers_corrected"] == 1


def test_validate_demotes_a_reddit_thread_cited_as_official_data():
    payload = _answer_payload(evidence=[
        {"tier": "A", "claim": "Everyone says rents doubled", "source": "r/oakland",
         "url": "https://www.reddit.com/r/oakland/comments/abc"},
    ])
    answer, notes = civic.validate(payload, _allowed("https://reddit.com/r/oakland/comments/abc"))
    # The claim survives, but only as a social signal -- which the ladder
    # renders as "what people are worried about", never as support.
    assert answer["tiers"] == ["F"]
    assert notes["tiers_corrected"] == 1


def test_validate_caps_a_think_tank_claiming_to_be_peer_reviewed():
    payload = _answer_payload(evidence=[
        {"tier": "B", "claim": "Supply cuts rents", "source": "Brookings",
         "url": "https://www.brookings.edu/articles/supply"},
    ])
    answer, _ = civic.validate(payload, _allowed("https://brookings.edu/articles/supply"))
    assert answer["tiers"] == ["C"]


def test_validate_trusts_a_university_host_that_claims_something_weaker():
    payload = _answer_payload(evidence=[
        {"tier": "E", "claim": "The university paper covered the vote",
         "source": "Campus paper", "url": "https://news.stanford.edu/story"},
    ])
    answer, _ = civic.validate(payload, _allowed("https://news.stanford.edu/story"))
    assert answer["tiers"] == ["E"]


def test_validate_reports_the_tiers_reached_in_ladder_order():
    answer, _ = civic.validate(_answer_payload(), _allowed(GOOD_URL, STUDY_URL, NEWS_URL))
    assert answer["tiers"] == ["A", "B", "E"]


def test_validate_keeps_urlless_actions_but_drops_invented_links():
    payload = _answer_payload(actions=[
        {"effort": 1, "what": "Email your council member", "why": "It is counted", "url": ""},
        {"effort": 2, "what": "Read the fabricated report", "why": "", "url": INVENTED},
    ])
    answer, notes = civic.validate(payload, _allowed(GOOD_URL))
    assert [a["what"] for a in answer["actions"]] == ["Email your council member"]
    assert notes["actions_dropped"] == 1


def test_validate_allows_only_one_two_minute_action():
    payload = _answer_payload(actions=[
        {"effort": 1, "what": "First quick thing", "why": "", "url": ""},
        {"effort": 1, "what": "Second quick thing", "why": "", "url": ""},
        {"effort": 3, "what": "Evening thing", "why": "", "url": ""},
        {"effort": 3, "what": "Another evening thing", "why": "", "url": ""},
        {"effort": 2, "what": "Hour thing", "why": "", "url": ""},
    ])
    answer, _ = civic.validate(payload, set())
    efforts = [a["effort"] for a in answer["actions"]]
    assert efforts == [1, 2, 3]          # sorted, and one slot each at 1 and 3


def test_validate_coerces_a_nonsense_confidence_to_low():
    payload = _answer_payload(mechanisms=[
        {"mechanism": "Something", "confidence": "very sure", "because": ""},
    ])
    answer, _ = civic.validate(payload, set())
    assert answer["mechanisms"][0]["confidence"] == "low"


def test_validate_caps_mechanisms_and_evidence():
    payload = _answer_payload(
        mechanisms=[{"mechanism": f"m{i}", "confidence": "high"} for i in range(9)],
        evidence=[{"tier": "A", "claim": f"c{i}", "source": "s", "url": GOOD_URL}
                  for i in range(20)],
    )
    answer, _ = civic.validate(payload, _allowed(GOOD_URL))
    assert len(answer["mechanisms"]) == civic.MAX_MECHANISMS
    assert len(answer["evidence"]) == civic.MAX_EVIDENCE


def test_validate_requires_a_headline():
    with pytest.raises(civic.CivicError) as exc:
        civic.validate(_answer_payload(headline="  "), set())
    assert exc.value.code == "no_headline"


# --------------------------------------------------------------------------
# synthesize : one call, one retry, no more
# --------------------------------------------------------------------------

def test_synthesize_returns_a_grounded_answer_in_one_call():
    calls = []

    def create(messages):
        calls.append(messages)
        return _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL),
                          _text_block(_answer_payload())])

    answer, notes = civic.synthesize("Why did my rent go up?", "Oakland, CA", create=create)
    assert len(calls) == 1
    assert answer["tiers"] == ["A", "B", "E"]
    assert notes["retried"] is False
    assert "Oakland, CA" in calls[0][0]["content"]


def test_synthesize_searches_harder_when_the_ladder_is_thin():
    thin = _answer_payload(evidence=[
        {"tier": "E", "claim": "Only journalism", "source": "Paper", "url": NEWS_URL},
    ])
    responses = [
        _Response([_search_block(NEWS_URL), _text_block(thin)]),
        _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL), _text_block(_answer_payload())]),
    ]
    sent = []

    def create(messages):
        sent.append(messages[0]["content"])
        return responses[len(sent) - 1]

    answer, notes = civic.synthesize("Why?", "Oakland, CA", create=create)
    assert len(sent) == 2
    assert "tiers A and B" in sent[1]
    assert answer["tiers"] == ["A", "B", "E"]   # the better pass wins
    assert notes["attempts"] == 2


def test_synthesize_keeps_the_thin_answer_when_the_retry_is_no_better():
    thin = _answer_payload(evidence=[
        {"tier": "E", "claim": "Only journalism", "source": "Paper", "url": NEWS_URL},
    ])

    def create(messages):
        return _Response([_search_block(NEWS_URL), _text_block(thin)])

    answer, _ = civic.synthesize("Why?", "", create=create)
    # An empty rung is an honest answer; two thin passes still return one.
    assert answer["tiers"] == ["E"]


def test_synthesize_retries_prose_once_then_raises_with_the_raw_text():
    calls = []

    def create(messages):
        calls.append(messages)
        return _Response([_text_block("I am afraid I could not find that.")])

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why?", "", create=create)
    assert len(calls) == 2
    assert exc.value.code == "not_json"
    assert "could not find" in exc.value.raw


def test_synthesize_does_not_retry_a_vague_question():
    vague = {"headline": civic.VAGUE_HEADLINE, "summary": "", "mechanisms": [],
             "evidence": [], "disputed": [], "live_decisions": [], "actions": [],
             "caveat": "", "rewrites": ["Why is my rent up?", "Who sets my property tax?",
                                        "What is on my ballot?"]}
    calls = []

    def create(messages):
        calls.append(messages)
        return _Response([_text_block(vague)])

    answer, _ = civic.synthesize("politics?", "", create=create)
    assert len(calls) == 1                     # searching harder finds nothing
    assert len(answer["rewrites"]) == 3


def test_synthesize_rejects_an_empty_question():
    with pytest.raises(civic.CivicError):
        civic.synthesize("   ", "", create=lambda m: None)


def test_synthesize_wraps_an_upstream_failure():
    def create(messages):
        raise RuntimeError("connection reset")

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why?", "", create=create)
    assert exc.value.code == "upstream_error"


def test_user_message_names_the_dropped_pin():
    msg = civic.user_message("Why?", "", lat=37.7749, lon=-122.4194)
    assert "37.77490" in msg and "-122.41940" in msg


def test_user_message_says_when_no_place_was_given():
    assert "national level" in civic.user_message("Why?", "")


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def test_cache_key_ignores_case_and_spacing():
    assert civic.cache_key("Why  is RENT up?", "Oakland") == \
           civic.cache_key("why is rent up?", "  oakland ")


def test_cache_key_separates_places():
    assert civic.cache_key("Why is rent up?", "Oakland") != \
           civic.cache_key("Why is rent up?", "Austin")


def test_cache_expires_after_24h():
    civic.cache_put("k", {"answer": 1}, now=1000.0)
    assert civic.cache_get("k", now=1000.0 + civic.CACHE_TTL_S - 1) == {"answer": 1}
    assert civic.cache_get("k", now=1000.0 + civic.CACHE_TTL_S + 1) is None


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------

_IP = iter(range(1, 250))


@pytest.fixture
def client():
    # A fresh forwarded IP per test: the rate-limit window is process-global
    # (backend/rate_limit.py), so sharing one would make unrelated tests
    # start failing as soon as this file grows past ten requests.
    with TestClient(app) as c:
        c.headers["X-Forwarded-For"] = f"198.51.100.{next(_IP)}"
        yield c


def _stub_synthesis(monkeypatch, answer=None, notes=None):
    answer = answer or civic.validate(_answer_payload(),
                                      _allowed(GOOD_URL, STUDY_URL, NEWS_URL))[0]
    calls = []

    def fake(question, location="", **kw):
        calls.append((question, location, kw))
        return answer, dict(notes or {"evidence_dropped": 0, "actions_dropped": 0,
                                      "mechanisms_dropped": 0, "latency_ms": 12,
                                      "retried": False, "attempts": 1})

    monkeypatch.setattr(civic, "available", lambda: True)
    monkeypatch.setattr(civic, "synthesize", fake)
    return calls


def test_ask_returns_an_answer_and_a_permalink_id(client, monkeypatch):
    calls = _stub_synthesis(monkeypatch)
    r = client.post("/api/civic/ask", json={"question": "Why is rent up?",
                                            "location": "Oakland, CA",
                                            "lat": 37.8, "lon": -122.27})
    assert r.status_code == 200, r.text
    body = r.json()
    # The id is a share token, not a cache id: it carries the question, so a
    # permalink works on the replica that never answered it.
    assert civic.read_token(body["id"]) == {"question": "Why is rent up?",
                                            "location": "Oakland, CA",
                                            "brief": False}
    assert body["cached"] is False
    assert body["answer"]["tiers"] == ["A", "B", "E"]
    assert calls[0][2]["lat"] == 37.8


def test_ask_serves_the_second_identical_question_from_cache(client, monkeypatch):
    calls = _stub_synthesis(monkeypatch)
    payload = {"question": "Why is rent up?", "location": "Oakland, CA"}
    client.post("/api/civic/ask", json=payload)
    r = client.post("/api/civic/ask", json=payload)
    assert r.json()["cached"] is True
    assert len(calls) == 1


def test_ask_rejects_an_empty_question(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    r = client.post("/api/civic/ask", json={"question": "   "})
    assert r.status_code == 400


def test_ask_says_so_when_the_key_is_missing(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: False)
    r = client.post("/api/civic/ask", json={"question": "Why is rent up?"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "unconfigured"


def test_ask_surfaces_the_raw_text_when_synthesis_fails(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: True)

    def boom(question, location="", **kw):
        raise civic.CivicError("prose, not JSON", raw="here is what I found", code="not_json")

    monkeypatch.setattr(civic, "synthesize", boom)
    r = client.post("/api/civic/ask", json={"question": "Why is rent up?"})
    assert r.status_code == 422
    assert r.json()["detail"]["raw"] == "here is what I found"


def test_ask_is_rate_limited_per_ip(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    codes = [client.post("/api/civic/ask",
                         json={"question": f"Question number {i}?"},
                         headers={"X-Forwarded-For": "203.0.113.9"}).status_code
             for i in range(12)]
    assert codes[:10] == [200] * 10
    assert codes[10:] == [429, 429]


def test_permalink_returns_the_cached_answer(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    posted = client.post("/api/civic/ask", json={"question": "Why is rent up?",
                                                 "location": "Oakland, CA"}).json()
    r = client.get(f"/api/civic/answer/{posted['id']}")
    assert r.status_code == 200
    assert r.json()["answer"]["headline"] == posted["answer"]["headline"]
    assert r.json()["question"] == "Why is rent up?"


def test_expired_permalink_is_a_404(client):
    assert client.get("/api/civic/answer/deadbeefdeadbeef").status_code == 404


def test_the_map_page_is_served_on_civic_and_on_a_permalink(client):
    for path in ("/civic", "/civic/r/abc123"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"]
        assert "The evidence, strongest first" in r.text
        assert "/api/civic/ask" in r.text


def test_a_broken_retry_does_not_erase_a_usable_first_answer():
    thin = _answer_payload(evidence=[
        {"tier": "E", "claim": "Only journalism", "source": "Paper", "url": NEWS_URL},
    ])
    responses = [
        _Response([_search_block(NEWS_URL), _text_block(thin)]),
        _Response([_text_block("On reflection I would rather write an essay.")]),
    ]
    seen = []

    def create(messages):
        seen.append(messages)
        return responses[len(seen) - 1]

    answer, _ = civic.synthesize("Why?", "", create=create)
    assert len(seen) == 2
    assert answer["tiers"] == ["E"]


# --------------------------------------------------------------------------
# Retrieval mode (EXA_API_KEY set) : we search, the model synthesizes
# --------------------------------------------------------------------------

def _results(*urls):
    return [{"tier": "A", "found_by": "A", "title": "t", "url": u,
             "host": "h", "published": "", "snippet": "s"} for u in urls]


def test_retrieval_puts_the_sources_in_the_prompt_and_asks_for_no_tools():
    seen = []

    def retrieve(question, location, *, harder=False):
        seen.append((question, location, harder))
        return _results(GOOD_URL, STUDY_URL, NEWS_URL)

    sent = []

    def create(messages):
        sent.append(messages[0]["content"])
        return _Response([_text_block(_answer_payload())])   # no search blocks

    answer, notes = civic.synthesize("Why is rent up?", "Oakland, CA",
                                     create=create, retrieve=retrieve)
    assert seen == [("Why is rent up?", "Oakland, CA", False)]
    assert "Search results you may use" in sent[0]
    assert GOOD_URL in sent[0]
    # Grounding now comes from what we retrieved, not from tool-result blocks.
    assert len(answer["evidence"]) == 3
    assert notes["sources"] == "exa"
    assert notes["sources_found"] == 3


def test_retrieval_drops_a_citation_we_never_retrieved():
    def retrieve(question, location, *, harder=False):
        return _results(GOOD_URL)

    def create(messages):
        return _Response([_text_block(_answer_payload(evidence=[
            {"tier": "A", "claim": "Real", "source": "Census", "url": GOOD_URL},
            {"tier": "A", "claim": "Invented", "source": "Census", "url": INVENTED},
        ]))])

    answer, _ = civic.synthesize("Why?", "", create=create, retrieve=retrieve)
    assert [e["claim"] for e in answer["evidence"]] == ["Real"]


def test_retrieval_retries_by_searching_harder():
    calls = []

    def retrieve(question, location, *, harder=False):
        calls.append(harder)
        return _results(NEWS_URL) if not harder else _results(GOOD_URL, STUDY_URL, NEWS_URL)

    def create(messages):
        payload = _answer_payload() if calls[-1] else _answer_payload(evidence=[
            {"tier": "E", "claim": "Only journalism", "source": "Paper", "url": NEWS_URL}])
        return _Response([_text_block(payload)])

    answer, notes = civic.synthesize("Why?", "", create=create, retrieve=retrieve)
    assert calls == [False, True]
    assert answer["tiers"] == ["A", "B", "E"]
    assert notes["retried"] is True


def test_empty_retrieval_falls_back_to_the_models_own_search():
    def retrieve(question, location, *, harder=False):
        return []           # bad key, quota, outage

    def create(messages):
        return _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL),
                          _text_block(_answer_payload())])

    answer, notes = civic.synthesize("Why?", "", create=create, retrieve=retrieve)
    assert notes["sources"] == "web_search"
    assert len(answer["evidence"]) == 3


# --------------------------------------------------------------------------
# The fence : Civic shares a process, a threadpool and two API keys with the
# CRM, so what it may take of them is capped and tested.
# --------------------------------------------------------------------------

from backend.routes import civic as civic_routes  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_budget():
    civic_routes._reset_daily_budget()
    yield
    civic_routes._reset_daily_budget()


def test_civic_imports_nothing_from_the_crm():
    """The only app module Civic may import is the per-IP rate limiter.

    This is the guarantee that a Civic change cannot break the CRM: no models,
    no db session, no auth, no agents. If this test fails, the surface stopped
    being standalone.
    """
    import ast
    import pathlib

    allowed = {"civic", "civic_sources", "civic_geo", "rate_limit"}
    root = pathlib.Path(__file__).resolve().parent.parent / "backend"
    for path in (root / "civic.py", root / "civic_sources.py", root / "civic_geo.py",
                 root / "routes" / "civic.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # `from . import x` / `from ..thing import y` -- the relative ones
            # are the only way to reach the rest of the app.
            if isinstance(node, ast.ImportFrom) and node.level:
                reached = {node.module} if node.module else {a.name for a in node.names}
                stray = {m for m in reached if m and m not in allowed}
                assert not stray, f"{path.name} imports {stray} from the app"


def test_only_two_syntheses_run_at_once(client, monkeypatch):
    """Past the cap the request is shed, not queued.

    A queued request holds a threadpool thread, and that pool is the CRM's.
    """
    _stub_synthesis(monkeypatch)
    held = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    try:
        assert all(held)
        r = client.post("/api/civic/ask", json={"question": "Why is rent up?"})
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "busy"
        assert r.headers["Retry-After"] == "20"
    finally:
        for _ in held:
            civic_routes._SLOTS.release()


def test_a_slot_is_returned_after_a_failed_synthesis(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: True)

    def boom(question, location="", **kw):
        raise civic.CivicError("nope", code="upstream_error")

    monkeypatch.setattr(civic, "synthesize", boom)
    for i in range(3):
        assert client.post("/api/civic/ask",
                           json={"question": f"Question {i}?"}).status_code == 422
    # Every slot is free again : a run of failures cannot wedge the surface.
    free = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    for _ in [f for f in free if f]:
        civic_routes._SLOTS.release()
    assert all(free)


def test_the_daily_budget_caps_what_civic_spends_of_the_shared_keys(client, monkeypatch):
    calls = _stub_synthesis(monkeypatch)
    monkeypatch.setattr(civic_routes, "_DAILY_CAP", 2)
    codes = [client.post("/api/civic/ask", json={"question": f"Question {i}?"}).status_code
             for i in range(3)]
    assert codes == [200, 200, 429]
    assert len(calls) == 2


def test_a_cached_answer_is_free_of_the_daily_budget(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    monkeypatch.setattr(civic_routes, "_DAILY_CAP", 1)
    payload = {"question": "Why is rent up?", "location": "Oakland, CA"}
    assert client.post("/api/civic/ask", json=payload).status_code == 200
    again = client.post("/api/civic/ask", json=payload)
    assert again.status_code == 200 and again.json()["cached"] is True


def test_civic_enabled_0_takes_the_whole_surface_down(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    monkeypatch.setenv("CIVIC_ENABLED", "0")
    assert client.get("/civic").status_code == 404
    assert client.get("/civic/r/abc123").status_code == 404
    r = client.post("/api/civic/ask", json={"question": "Why is rent up?"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "disabled"


def test_the_rest_of_the_app_still_serves_when_civic_is_off(client, monkeypatch):
    monkeypatch.setenv("CIVIC_ENABLED", "0")
    assert client.get("/api/health").status_code == 200


def test_a_shed_request_does_not_spend_the_daily_budget(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    monkeypatch.setattr(civic_routes, "_DAILY_CAP", 1)
    held = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    try:
        assert client.post("/api/civic/ask",
                           json={"question": "Shed me?"}).status_code == 503
    finally:
        for _ in [h for h in held if h]:
            civic_routes._SLOTS.release()
    # The one answer in today's budget is still there.
    assert client.post("/api/civic/ask",
                       json={"question": "Why is rent up?"}).status_code == 200


# --------------------------------------------------------------------------
# Streaming : the same answer, but the page can say what is happening
# --------------------------------------------------------------------------

def _events(response_text):
    """Parse an SSE body into [(event, data), ...]."""
    out = []
    for block in response_text.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


def test_the_stream_reports_progress_and_then_the_answer(client, monkeypatch):
    answer = civic.validate(_answer_payload(), _allowed(GOOD_URL, STUDY_URL, NEWS_URL))[0]

    def fake(question, location="", *, on_event=None, **kw):
        on_event("started")
        on_event("retrieved", count=24, tiers=["A", "B", "E"])
        on_event("headline", text=answer["headline"])
        return answer, {"evidence_dropped": 0, "actions_dropped": 0,
                        "mechanisms_dropped": 0, "latency_ms": 10,
                        "sources": "exa", "sources_found": 24, "retried": False}

    monkeypatch.setattr(civic, "available", lambda: True)
    monkeypatch.setattr(civic, "synthesize", fake)

    r = client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _events(r.text)
    names = [name for name, _ in events]
    assert names == ["started", "retrieved", "headline", "answer"]
    # The headline arrives before the answer : that is the whole point.
    assert events[2][1]["text"] == answer["headline"]
    assert events[-1][1]["answer"]["tiers"] == ["A", "B", "E"]
    assert events[-1][1]["cached"] is False


def test_the_stream_serves_a_cached_answer_immediately(client, monkeypatch):
    calls = _stub_synthesis(monkeypatch)
    payload = {"question": "Why is rent up?", "location": "Oakland, CA"}
    client.post("/api/civic/ask", json=payload)
    r = client.post("/api/civic/ask/stream", json=payload)
    events = _events(r.text)
    assert [name for name, _ in events] == ["answer"]
    assert events[0][1]["cached"] is True
    assert len(calls) == 1


def test_the_stream_ends_with_an_error_event_not_a_dead_connection(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: True)

    def boom(question, location="", *, on_event=None, **kw):
        on_event("started")
        raise civic.CivicError("prose, not JSON", raw="what we found", code="not_json")

    monkeypatch.setattr(civic, "synthesize", boom)
    r = client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
    events = _events(r.text)
    assert [name for name, _ in events] == ["started", "error"]
    assert events[-1][1]["raw"] == "what we found"


def test_the_stream_releases_its_slot_when_synthesis_fails(client, monkeypatch):
    monkeypatch.setattr(civic, "available", lambda: True)

    def boom(question, location="", *, on_event=None, **kw):
        raise civic.CivicError("nope", code="upstream_error")

    monkeypatch.setattr(civic, "synthesize", boom)
    client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
    free = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    for _ in [f for f in free if f]:
        civic_routes._SLOTS.release()
    assert all(free)


def test_the_stream_obeys_the_same_fence(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    held = [civic_routes._SLOTS.acquire(blocking=False)
            for _ in range(civic_routes._MAX_CONCURRENCY)]
    try:
        r = client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
        assert r.status_code == 503 and r.json()["detail"]["code"] == "busy"
    finally:
        for _ in [h for h in held if h]:
            civic_routes._SLOTS.release()

    monkeypatch.setenv("CIVIC_ENABLED", "0")
    off = client.post("/api/civic/ask/stream", json={"question": "Why is rent up?"})
    assert off.status_code == 503 and off.json()["detail"]["code"] == "disabled"


# --------------------------------------------------------------------------
# The streaming call itself : the part that talks to the SDK
# --------------------------------------------------------------------------

class _Block:
    def __init__(self, type_): self.type = type_


class _Event:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Delta:
    def __init__(self, text): self.text = text


class _FakeStream:
    def __init__(self, events, final):
        self._events, self._final = events, final

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(self._events)
    def get_final_message(self): return self._final


class _FakeClient:
    def __init__(self, events, final):
        self._events, self._final = events, final
        self.kwargs = None

    def with_options(self, **kw): return self

    @property
    def messages(self): return self

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return _FakeStream(self._events, self._final)

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self._final


def test_the_streaming_call_reports_searches_writing_and_the_headline(monkeypatch):
    payload = _answer_payload()
    body = json.dumps(payload)
    events = [
        _Event("content_block_start", content_block=_Block("server_tool_use")),
        _Event("content_block_start", content_block=_Block("web_search_tool_result")),
        _Event("content_block_start", content_block=_Block("server_tool_use")),
        _Event("content_block_start", content_block=_Block("text")),
        # The headline is the first key, so it completes early in the write.
        _Event("content_block_delta", delta=_Delta(body[:120])),
        _Event("content_block_delta", delta=_Delta(body[120:])),
    ]
    final = _Response([_search_block(GOOD_URL), _text_block(payload)])
    fake = _FakeClient(events, final)
    monkeypatch.setattr(civic, "_client", lambda: fake)

    seen = []
    response = civic._create([{"role": "user", "content": "x"}],
                             on_event=lambda name, **f: seen.append((name, f)))

    assert response.content == final.content        # one turn, passed through
    names = [n for n, _ in seen]
    assert names.count("searching") == 2
    assert "writing" in names
    assert ("headline", {"text": payload["headline"]}) in seen


def test_without_a_callback_the_call_does_not_stream(monkeypatch):
    final = _Response([_text_block(_answer_payload())])
    fake = _FakeClient([], final)
    monkeypatch.setattr(civic, "_client", lambda: fake)
    assert civic._create([{"role": "user", "content": "x"}]).content == final.content
    assert "tools" in fake.kwargs          # web_search mode by default


def test_retrieval_mode_sends_no_tools(monkeypatch):
    final = _Response([_text_block(_answer_payload())])
    fake = _FakeClient([], final)
    monkeypatch.setattr(civic, "_client", lambda: fake)
    civic._create([{"role": "user", "content": "x"}], retrieval=True)
    assert "tools" not in fake.kwargs


def test_effort_rides_in_extra_body_only_when_configured(monkeypatch):
    final = _Response([_text_block(_answer_payload())])
    fake = _FakeClient([], final)
    monkeypatch.setattr(civic, "_client", lambda: fake)

    civic._create([{"role": "user", "content": "x"}])
    assert "extra_body" not in fake.kwargs          # unset by default

    monkeypatch.setattr(civic, "EFFORT", "low")
    civic._create([{"role": "user", "content": "x"}])
    assert fake.kwargs["extra_body"] == {"output_config": {"effort": "low"}}


@pytest.mark.parametrize("partial,expected", [
    ('{"headline": "Rents rose because supply stalled."', "Rents rose because supply stalled."),
    ('{"headline": "A \\"quoted\\" claim", "summary"', 'A "quoted" claim'),
    ('{"headl', ""),
    ('{"headline": "half writ', ""),
])
def test_peek_headline_reads_a_half_written_answer(partial, expected):
    assert civic._peek_headline(partial) == expected


# --------------------------------------------------------------------------
# When every question fails, the surface has to be able to say why
# --------------------------------------------------------------------------

class _ModelGone(Exception):
    status_code = 404

    def __str__(self):
        return "model: claude-sonnet-5 not_found_error"


def test_an_unusable_model_falls_back_to_the_one_the_app_already_runs(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    monkeypatch.setattr(civic, "MODEL", "claude-sonnet-5")
    seen = []

    def create(messages):
        seen.append(civic.active_model())
        if civic.active_model() != civic.FALLBACK_MODEL:
            raise _ModelGone()
        return _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL),
                          _text_block(_answer_payload())])

    answer, _ = civic.synthesize("Why is rent up?", "", create=create)
    assert seen == ["claude-sonnet-5", civic.FALLBACK_MODEL]
    assert len(answer["evidence"]) == 3


def test_a_failure_that_is_not_about_the_model_is_not_retried(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    calls = []

    def create(messages):
        calls.append(1)
        raise RuntimeError("connection reset by peer")

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why?", "", create=create)
    assert len(calls) == 1
    assert exc.value.code == "upstream_error"
    # The technical cause travels in `raw`, so the page shows a plain sentence
    # and keeps the detail behind "show what we found".
    assert "RuntimeError" in exc.value.raw
    assert str(exc.value) == "The search could not be completed."


def test_the_last_failure_is_remembered_for_selftest(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        raise RuntimeError("connection reset by peer")

    with pytest.raises(civic.CivicError):
        civic.synthesize("Why?", "", create=create)
    assert civic.LAST_ERROR["type"] == "RuntimeError"
    assert "connection reset" in civic.LAST_ERROR["message"]
    assert civic.LAST_ERROR["mode"] in ("exa", "web_search")


def test_selftest_reports_configuration_and_the_last_failure(client, monkeypatch):
    civic.LAST_ERROR.clear()
    civic.LAST_ERROR.update({"type": "NotFoundError", "message": "model: nope",
                             "status": 404, "model": "nope", "mode": "web_search"})
    monkeypatch.setenv("EXA_API_KEY", "")
    r = client.get("/api/civic/selftest")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["sources"] == "keyless"      # no Exa key, so the fan-out only
    assert body["model_in_use"] == civic.active_model()
    assert body["last_error"]["type"] == "NotFoundError"
    # No key material, ever: the key fields say whether one is set, never what
    # it is, and nothing in the payload looks like a token.
    assert body["anthropic_key"] is True or body["anthropic_key"] is False
    assert body["exa_key"] is True or body["exa_key"] is False
    blob = json.dumps(body)
    assert "sk-ant" not in blob
    assert not re.search(r"[A-Za-z0-9_-]{32,}", blob)
    civic.LAST_ERROR.clear()


class _ToolNotEnabled(Exception):
    status_code = 400

    def __str__(self):
        return "tools.0: web_search_20260209 is not available to this account"


def test_a_missing_web_search_entitlement_says_what_to_do(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        raise _ToolNotEnabled()

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why is rent up?", "", create=create)
    assert exc.value.code == "no_search_backend"
    assert "EXA_API_KEY" in str(exc.value)
    # Never the workaround of answering from memory: an unsourced answer is
    # the failure this product exists to prevent.
    assert "web_search tool" in str(exc.value)


# --------------------------------------------------------------------------
# Dropping a pin is a briefing, not a vague question
# --------------------------------------------------------------------------

def test_a_briefing_tells_the_model_not_to_ask_for_a_narrower_question():
    msg = civic.user_message("What is happening in Oakland right now?",
                             "Oakland, California", brief=True)
    assert "place briefing" in msg
    assert "Do NOT ask for a narrower question" in msg
    assert "rewrites" in msg


def test_an_ordinary_question_is_not_a_briefing():
    assert "place briefing" not in civic.user_message("Why is my rent up?", "Oakland")


def test_the_brief_flag_reaches_the_prompt(client, monkeypatch):
    seen = {}

    def fake(question, location="", **kw):
        seen.update(kw)
        return civic.validate(_answer_payload(), _allowed(GOOD_URL, STUDY_URL, NEWS_URL))[0], {
            "evidence_dropped": 0, "actions_dropped": 0, "mechanisms_dropped": 0,
            "latency_ms": 5, "sources": "exa", "sources_found": 12, "retried": False}

    monkeypatch.setattr(civic, "available", lambda: True)
    monkeypatch.setattr(civic, "synthesize", fake)
    r = client.post("/api/civic/ask", json={
        "question": "What is happening in Oakland right now?",
        "location": "Oakland, CA", "brief": True})
    assert r.status_code == 200
    assert seen["brief"] is True


def test_selftest_names_the_replica_and_its_recent_failures(client):
    civic.RECENT_ERRORS.clear()
    civic.LAST_ERROR.clear()
    body = client.get("/api/civic/selftest").json()
    assert body["boot"] == civic.BOOT_ID
    assert body["uptime_s"] >= 0
    assert body["recent_errors"] == []
    assert "sources_last_run" in body


def test_every_failure_is_kept_in_the_ring_buffer(monkeypatch):
    civic.RECENT_ERRORS.clear()
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        raise RuntimeError("connection reset by peer")

    for _ in range(3):
        with pytest.raises(civic.CivicError):
            civic.synthesize("Why?", "", create=create)
    assert len(civic.RECENT_ERRORS) == 3
    assert all(e["boot"] == civic.BOOT_ID for e in civic.RECENT_ERRORS)
    civic.RECENT_ERRORS.clear()


# --------------------------------------------------------------------------
# A long search loop pauses the turn. Resuming it is the difference between
# an answer and "the model answered in prose".
# --------------------------------------------------------------------------

class _Paused:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _TurnClient:
    """Returns a queued response per call, recording what it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []

    def with_options(self, **kw): return self

    @property
    def messages(self): return self

    def create(self, **kwargs):
        self.sent.append(kwargs["messages"])
        return self._responses.pop(0)


def test_a_paused_turn_is_resumed_and_its_searches_are_kept(monkeypatch):
    payload = _answer_payload()
    fake = _TurnClient([
        # Searched, ran out of turn before writing anything.
        _Paused([_search_block(GOOD_URL, STUDY_URL)], "pause_turn"),
        # Resumed: more searching, then the answer.
        _Paused([_search_block(NEWS_URL), _text_block(payload)], "end_turn"),
    ])
    monkeypatch.setattr(civic, "_client", lambda: fake)

    response = civic._create([{"role": "user", "content": "Why is rent up?"}])
    assert response.stop_reason == "end_turn"
    # Both turns' blocks are kept, so the citations found before the pause
    # still count as grounded.
    urls = civic.search_urls(response.content)
    assert civic.normalise_url(GOOD_URL) in urls
    assert civic.normalise_url(NEWS_URL) in urls
    # The second call carried the first turn's content back as an assistant turn.
    assert len(fake.sent) == 2
    assert fake.sent[1][-1]["role"] == "assistant"


def test_resuming_gives_up_rather_than_looping_forever(monkeypatch):
    fake = _TurnClient([_Paused([_search_block(GOOD_URL)], "pause_turn")
                        for _ in range(civic.MAX_TURNS + 2)])
    monkeypatch.setattr(civic, "_client", lambda: fake)
    response = civic._create([{"role": "user", "content": "x"}])
    assert len(fake.sent) == civic.MAX_TURNS
    assert response.stop_reason == "pause_turn"


def test_a_cut_off_answer_says_so_instead_of_blaming_prose(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        return _Paused([_search_block(GOOD_URL),
                        _text_block('{"headline": "Rents rose because')], "max_tokens")

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why is rent up?", "", create=create)
    assert exc.value.code == "truncated"
    assert "cut off" in str(exc.value)


def test_an_unfinished_search_says_so_too(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        return _Paused([_search_block(GOOD_URL)], "pause_turn")

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("Why is rent up?", "", create=create)
    assert exc.value.code == "unfinished"


# --------------------------------------------------------------------------
# An answer with nothing under it is the one thing this must never publish
# --------------------------------------------------------------------------

def _tool_error_block(code="max_uses_exceeded"):
    """What a failed web_search looks like: HTTP 200, error object inside."""
    return {"type": "web_search_tool_result",
            "content": {"type": "web_search_tool_result_error", "error_code": code}}


def test_a_failed_web_search_stops_the_answer_instead_of_writing_from_memory(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    # The model happily writes prose with no citations when its search fails.
    unsourced = _answer_payload(evidence=[], headline="Live search wasn't available")

    def create(messages):
        return _Response([_tool_error_block("web_search_unavailable"),
                          _text_block(unsourced)])

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("What is proposed near Mira Mesa Blvd?", "San Diego", create=create)
    assert exc.value.code == "no_search_backend"
    assert "web_search_unavailable" in str(exc.value)


def test_search_errors_are_read_off_the_tool_result_block():
    assert civic.search_errors([_tool_error_block("max_uses_exceeded")]) == ["max_uses_exceeded"]
    # A healthy result carries a LIST of results, and is not an error.
    assert civic.search_errors([_search_block(GOOD_URL)]) == []


def test_an_answer_with_an_empty_ladder_is_refused(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")

    def create(messages):
        # Searches ran, nothing was cited: every claim came from the model.
        return _Response([_search_block(GOOD_URL),
                          _text_block(_answer_payload(evidence=[]))])

    with pytest.raises(civic.CivicError) as exc:
        civic.synthesize("What is proposed near Mira Mesa Blvd?", "San Diego", create=create)
    assert exc.value.code == "no_evidence"
    assert "answer from memory" in str(exc.value)


def test_a_vague_question_is_still_allowed_to_have_no_evidence(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    vague = {"headline": civic.VAGUE_HEADLINE, "summary": "", "mechanisms": [],
             "evidence": [], "disputed": [], "live_decisions": [], "actions": [],
             "caveat": "", "rewrites": ["Why is my rent up?"]}

    def create(messages):
        return _Response([_text_block(vague)])

    answer, _ = civic.synthesize("politics?", "", create=create)
    assert answer["headline"] == civic.VAGUE_HEADLINE


def test_one_sourced_pass_survives_an_unsourced_one(monkeypatch):
    monkeypatch.setattr(civic, "_model_override", "")
    payloads = [_answer_payload(evidence=[]),                     # thin first pass
                _answer_payload()]                                 # sourced retry
    calls = []

    def create(messages):
        calls.append(1)
        return _Response([_search_block(GOOD_URL, STUDY_URL, NEWS_URL),
                          _text_block(payloads[len(calls) - 1])])

    answer, _ = civic.synthesize("Why?", "", create=create)
    assert len(answer["evidence"]) == 3


# --------------------------------------------------------------------------
# Permalinks : two replicas, one link, no database
# --------------------------------------------------------------------------

def test_a_share_token_round_trips():
    token = civic.share_token("Why did my rent go up 15%?", "Oakland, CA", brief=True)
    assert civic.read_token(token) == {"question": "Why did my rent go up 15%?",
                                       "location": "Oakland, CA", "brief": True}


@pytest.mark.parametrize("bad", ["", "   ", "not a token", "abc123def4567890",
                                 "x" * 3000, "!!!!"])
def test_read_token_refuses_anything_that_is_not_one(bad):
    assert civic.read_token(bad) is None


def test_read_token_will_not_unpack_a_bomb():
    # A few hundred bytes of base64 must not become a gigabyte of JSON.
    import base64 as b64
    import zlib as z
    bomb = b64.urlsafe_b64encode(z.compress(b"[" + b"0," * 2_000_000 + b"0]")).decode()
    assert civic.read_token(bomb.rstrip("=")) is None


def test_a_permalink_this_replica_never_answered_hands_back_the_question(client):
    # The exact production case: two replicas, and the link lands on the one
    # without the answer (or on any replica after a deploy).
    civic.cache_clear()
    token = civic.share_token("Why is rent up?", "Oakland, CA")
    r = client.get(f"/api/civic/answer/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["rebuild"] is True
    assert body["answer"] is None
    assert body["question"] == "Why is rent up?"
    assert body["location"] == "Oakland, CA"


def test_a_permalink_this_replica_did_answer_is_served_from_cache(client, monkeypatch):
    _stub_synthesis(monkeypatch)
    posted = client.post("/api/civic/ask", json={"question": "Why is rent up?",
                                                 "location": "Oakland, CA"}).json()
    r = client.get(f"/api/civic/answer/{posted['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["rebuild"] is False
    assert body["answer"]["headline"] == posted["answer"]["headline"]


def test_a_link_that_is_not_ours_is_still_a_404(client):
    assert client.get("/api/civic/answer/deadbeefdeadbeef").status_code == 404


# --------------------------------------------------------------------------
# The fast lane: tap a thing on the map, no model involved
# --------------------------------------------------------------------------

def test_the_place_endpoint_returns_headlines_without_calling_the_model(
        client, monkeypatch):
    called = []
    monkeypatch.setattr(civic, "synthesize",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr(civic_sources, "headlines", lambda subject, near="", limit=6: {
        "items": [{"tier": "E", "title": "School board votes on the budget",
                   "url": "https://www.mercurynews.com/story", "host": "mercurynews.com",
                   "published": "2026-03-02", "snippet": "", "found_by": "google_news"}],
        "ran": {"google_news": 1, "gdelt": "rate_limited"}})

    r = client.get("/api/civic/place?subject=Lincoln Elementary&near=Oakland, CA")
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["host"] == "mercurynews.com"
    assert body["ran"]["gdelt"] == "rate_limited"
    assert called == []                 # the fast lane never synthesizes


def test_the_place_endpoint_needs_a_subject(client):
    assert client.get("/api/civic/place?subject=%20%20").status_code == 400


def test_the_place_endpoint_is_off_when_civic_is(client, monkeypatch):
    monkeypatch.setenv("CIVIC_ENABLED", "0")
    assert client.get("/api/civic/place?subject=Lincoln").status_code == 503


# --------------------------------------------------------------------------
# The stack of governments over a point
# --------------------------------------------------------------------------

from backend import civic_geo  # noqa: E402

_CENSUS = {
    "Congressional Districts": [{"NAME": "Congressional District 12", "GEOID": "0612"}],
    "State Legislative Districts - Upper Chamber": [{"NAME": "State Senate District 7"}],
    "State Legislative Districts - Lower Chamber": [{"NAME": "Assembly District 18"}],
    "Counties": [{"NAME": "Alameda County"}],
    "Incorporated Places": [{"NAME": "Oakland"}],
    "Unified School Districts": [{"NAME": "Oakland Unified School District"}],
    "Census Tracts": [{"NAME": "Census Tract 4030"}],
}

_OSM = [
    {"type": "relation", "id": 77, "tags": {"boundary": "political",
     "political_division": "city_council", "name": "Council District 3"}},
    {"type": "relation", "id": 12, "tags": {"boundary": "administrative",
     "admin_level": "8", "name": "Oakland"}},
    {"type": "way", "id": 9, "tags": {"landuse": "residential"}},
]


def _stack(monkeypatch, census=None, osm=None):
    civic_geo._CACHE.clear()
    monkeypatch.setattr(civic_geo, "census_geographies",
                        lambda lat, lon: census if census is not None else _CENSUS)
    monkeypatch.setattr(civic_geo, "osm_areas",
                        lambda lat, lon: osm if osm is not None else _OSM)
    return civic_geo.stack(37.8044, -122.2712)


def test_the_stack_names_every_layer_closest_to_home_first(monkeypatch):
    found = _stack(monkeypatch)
    assert [l["key"] for l in found["layers"]] == [
        "landuse", "council", "place", "school", "county",
        "state_lower", "state_upper", "congress"]
    by_key = {l["key"]: l for l in found["layers"]}
    assert by_key["congress"]["name"] == "Congressional District 12"
    assert by_key["council"]["name"] == "Council District 3"      # OSM only
    assert by_key["school"]["name"] == "Oakland Unified School District"


def test_every_layer_says_what_it_decides(monkeypatch):
    for layer in _stack(monkeypatch)["layers"]:
        assert layer["decides"], layer["key"]
        assert layer["elected"]
        assert layer["color"].startswith("#")


def test_the_council_district_carries_the_relation_the_outline_needs(monkeypatch):
    council = next(l for l in _stack(monkeypatch)["layers"] if l["key"] == "council")
    assert council["relation"] == 77
    assert council["source"] == "openstreetmap"


def test_land_use_says_plainly_that_it_is_not_the_zoning_code(monkeypatch):
    landuse = next(l for l in _stack(monkeypatch)["layers"] if l["key"] == "landuse")
    assert "not the legal zoning code" in landuse["detail"]


def test_one_source_failing_still_names_what_the_other_knows(monkeypatch):
    civic_geo._CACHE.clear()

    def boom(lat, lon):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(civic_geo, "census_geographies", boom)
    monkeypatch.setattr(civic_geo, "osm_areas", lambda lat, lon: _OSM)
    found = civic_geo.stack(37.8, -122.2)
    assert [l["key"] for l in found["layers"]] == ["landuse", "council", "place"]
    assert found["sources"]["census"] == "RuntimeError"


def test_the_question_speaks_the_layer_s_own_vocabulary(monkeypatch):
    by_key = {l["key"]: l for l in _stack(monkeypatch)["layers"]}
    school = civic_geo.question_for(by_key["school"], "Oakland")
    assert "Oakland Unified School District" in school
    assert "boundaries" in school
    congress = civic_geo.question_for(by_key["congress"], "Oakland")
    assert "Congressional District 12" in congress and "Oakland" in congress


def test_the_jurisdictions_endpoint_returns_the_stack(client, monkeypatch):
    _stack(monkeypatch)
    civic_geo._CACHE.clear()
    r = client.get("/api/civic/jurisdictions?lat=37.8044&lon=-122.2712")
    assert r.status_code == 200
    assert {l["key"] for l in r.json()["layers"]} >= {"place", "school", "congress"}


def test_the_jurisdictions_endpoint_rejects_a_point_off_the_earth(client):
    assert client.get("/api/civic/jurisdictions?lat=999&lon=0").status_code == 400


def test_a_census_named_district_is_outlined_by_looking_it_up(monkeypatch):
    calls = []

    def fake_outline(relation_id):
        calls.append(relation_id)
        return {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]]]}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"elements": [{"type": "relation", "id": 4242}]}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, data=None, headers=None): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(civic_geo, "outline", fake_outline)
    shape = civic_geo.outline_by_name("Oakland Unified School District")
    assert calls == [4242]
    assert shape["type"] == "MultiLineString"


@pytest.mark.parametrize("bad", ["", "   ", 'name" with a quote', "back\\slash"])
def test_outline_by_name_refuses_anything_that_could_escape_the_query(bad):
    # The name goes inside an Overpass query string, so it is checked before
    # it gets there rather than escaped afterwards.
    assert civic_geo.outline_by_name(bad) == {}
