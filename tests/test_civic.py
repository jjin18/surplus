"""
Civic policy search: the rules that make the answer trustworthy.

The evidence hierarchy is a product rule, not a prompt suggestion, so the
tests that matter here are the ones that hold when the model misbehaves: an
invented URL is dropped, a thin answer is searched again, prose instead of
JSON fails loudly with the raw text kept for the UI.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend import civic
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
    assert body["id"] == civic.cache_key("Why is rent up?", "Oakland, CA")
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

    allowed = {"civic", "civic_sources", "rate_limit"}
    root = pathlib.Path(__file__).resolve().parent.parent / "backend"
    for path in (root / "civic.py", root / "civic_sources.py", root / "routes" / "civic.py"):
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
