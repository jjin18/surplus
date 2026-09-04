"""
civic.py : the evidence-synthesis engine behind Civic policy search.

One question in ("why did my rent go up 15%?"), one Claude call with web
search out, and a JSON answer whose every citation is a URL the model
actually saw in a search result.

The product rule that makes this worth building is the evidence hierarchy:

    A  official data          census, agency stats, court + legislative records
    B  peer-reviewed research causal studies, meta-analyses, working papers
    C  institutional research think tanks, universities, legislative offices
    D  expert analysis        named domain experts, professional bodies
    E  journalism             established outlets
    F  social signal          X, Reddit, forums

A and B can carry a number on their own. F is NEVER evidence of a claim, only
evidence that people care. Everything in between is supporting. The ladder is
enforced twice : once in the prompt, and once here in `validate()`, because a
rule that only lives in a prompt is a suggestion.

Grounding is the other hard rule : an evidence item whose URL did not appear
in a `web_search_tool_result` block is dropped, not shown. That is the only
defence we have against a confidently fabricated number, so it lives in code.

Nothing here touches the DB or the session. The route module owns the HTTP
concerns (rate limit, cache, permalinks); this module owns the prompt, the
grounding check and the schema.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from . import civic_sources

try:
    import anthropic
    _SDK_AVAILABLE = True
except ImportError:  # the app must import without the SDK installed
    _SDK_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TIER_ORDER = ("A", "B", "C", "D", "E", "F")

TIER_LABEL: dict[str, str] = {
    "A": "Official data",
    "B": "Peer-reviewed research",
    "C": "Institutional research",
    "D": "Expert analysis",
    "E": "Journalism",
    "F": "Social signal",
}

# Sonnet 5 : same family the rest of the app uses for search work, one
# generation on from the 4.6 the prototype named -- cheaper per token and it
# supports the current web_search tool. Haiku 4.5 400s on that tool version.
MODEL = (os.environ.get("CIVIC_MODEL") or "").strip() or "claude-sonnet-5"

# 4000, not the prototype's 1000 : a full answer is ~12 evidence items plus
# mechanisms and actions, and a truncated response is unparseable JSON.
MAX_TOKENS = 4000

# The model is told to work A -> F, so it needs several searches. 8 is the
# ceiling, not a target ; most answers settle in 4-6.
_DEFAULT_MAX_USES = 8

# One query is 15-25s of search + synthesis. The SDK default (10 minutes) is
# not a useful failure signal, and this surface shares its threadpool with the
# CRM : a call that has not come back in 75s is holding a thread someone else
# needs, so give up and say so.
REQUEST_TIMEOUT_S = 75.0

# Only search again if the first pass left time for it. Bounds one request at
# roughly RETRY_DEADLINE_S + REQUEST_TIMEOUT_S instead of twice the timeout.
RETRY_DEADLINE_S = 45.0

# Answers must span at least this many tiers or we search again, harder.
MIN_TIERS = 3

# Spec constraints on a well-formed answer.
MAX_MECHANISMS = 6
MAX_EVIDENCE = 12
MAX_REWRITES = 3

CACHE_TTL_S = 24 * 3600

# Headline the model is told to use when the question is too vague to answer.
VAGUE_HEADLINE = "Need a more specific question"


def _max_uses() -> int:
    try:
        return max(1, int(os.environ.get("CIVIC_MAX_SEARCHES", _DEFAULT_MAX_USES)))
    except ValueError:
        return _DEFAULT_MAX_USES


def _web_search_tool() -> dict:
    return {"type": "web_search_20260209", "name": "web_search", "max_uses": _max_uses()}


def _api_key() -> str:
    # Stripped for the same reason backend/agents/llm.py strips it : a pasted
    # Railway value carries a trailing newline, which httpx rejects as an
    # illegal header value long before the request reaches Anthropic.
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def available() -> bool:
    """True when a live answer can be produced (SDK importable + key set)."""
    return _SDK_AVAILABLE and bool(_api_key())


_CLIENT: Optional["anthropic.Anthropic"] = None


def _client() -> "anthropic.Anthropic":
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(api_key=_api_key(), max_retries=1)
    return _CLIENT


class CivicError(RuntimeError):
    """A query that could not be turned into an answer.

    `raw` carries whatever the model did say, so the UI can offer the
    "show what we found" toggle instead of a dead end.
    """

    def __init__(self, message: str, *, raw: str = "", code: str = "synthesis_failed"):
        super().__init__(message)
        self.raw = raw
        self.code = code


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_HIERARCHY_TABLE = """
A  Official data           census, agency statistics, election results, court and legislative records
B  Peer-reviewed research  causal studies, meta-analyses, working papers
C  Institutional research  think tanks, universities, legislative research offices
D  Expert analysis         named domain experts, professional bodies
E  Journalism              established outlets; the best source for "what is happening right now"
F  Social signal           X, Reddit, forums
""".strip()

_SCHEMA_BLOCK = """{
 "headline": "one sentence that answers the question directly",
 "summary": "2-4 sentences: what is happening, to whom, since when",
 "mechanisms": [{"mechanism":"one cause, in plain words","confidence":"high|medium|low","because":"one sentence on why you believe it"}],
 "evidence": [{"tier":"A|B|C|D|E|F","claim":"one specific finding, with the number if there is one","source":"who published it","url":"https://..."}],
 "disputed": ["one claim credible sources disagree about, and what each side says"],
 "live_decisions": ["one vote, hearing, comment period or election that is open or coming, with the date if you know it"],
 "actions": [{"effort":1,"what":"something the reader can actually do","why":"what it changes","url":"https://... or empty"}],
 "caveat": "one sentence on what this evidence cannot tell you",
 "rewrites": ["only if the question is too vague to answer: three sharper questions to ask instead"]
}"""

# How the answer should read. A resident asked this question because something
# happened to them, not because they wanted a policy briefing.
_STYLE_RULES = """Write for the person who asked, not for a policy analyst:
- Short sentences. One idea each.
- Everyday words. If a technical term is unavoidable, define it in the same
  sentence -- "the assessment ratio (the share of your home's value that gets
  taxed)".
- Spell out an acronym the first time you use it.
- Give the number and the year together: "up 15% since 2023", never "up
  significantly in recent years".
- Say who did the thing. "The county reassessed every home", not "a
  reassessment was undertaken".
- No hedging stacks ("it may potentially be somewhat"), no throat-clearing
  ("it is important to note"), no summary of what you are about to say."""


def system_prompt(today: Optional[date] = None, *, retrieval: bool = False) -> str:
    """The role, the ladder, and the rules that are not negotiable.

    `retrieval=True` is the mode where we did the searching (Exa) and hand the
    results over; otherwise the model searches for itself with `web_search`.
    """
    today = today or date.today()
    if retrieval:
        sourcing = """You will be given a numbered list of search results: every source you are
allowed to use. Read them and answer from them.
- Cite only URLs from that list, copied exactly. Never write a URL that is
  not in it.
- Never state a number that is not in one of those results.
- If the results do not answer the question, say so. A thin answer that is
  true beats a full one that is invented."""
    else:
        sourcing = """Search the web before you answer, working down the ladder from A to F.
- Cite only URLs you actually saw in a search result, copied exactly.
- Never state a number you did not find, and never write a URL you did not
  see."""

    return f"""You are a civic evidence-synthesis engine. Today is {today:%B %-d, %Y}.

Someone wants to know why something is happening where they live. Your job is
to gather what is already known -- official data, research, institutions,
journalism -- and lay it out honestly, ranked by how it was produced.

Rank every source on this ladder:

{_HIERARCHY_TABLE}

{sourcing}

Rules about the ladder:
- Tiers A and B can carry a number on their own. C, D and E support a claim
  but do not settle it.
- Tier F is never evidence that something is true. It is only evidence that
  people are talking about it, so use it only to describe what people are
  asking or arguing about.
- If nothing at tier A or B supports your headline, hedge the headline --
  "likely", "the available evidence suggests" -- rather than stating it flat.
- An empty tier is a real answer. Leave it empty rather than filling it with
  something weaker.
- On a contested topic, put both sides in `disputed` and keep the headline
  neutral. Rank sources by how they were produced, never by who agrees with
  them.
- If the question is too vague to answer (for example "politics?"), set
  headline to "{VAGUE_HEADLINE}", leave the lists empty, and put three
  sharper questions in `rewrites`.

{_STYLE_RULES}

Reply with ONLY a JSON object -- no prose around it, no markdown fences:

{_SCHEMA_BLOCK}

Shape: 3-6 mechanisms. 5-12 evidence items across at least three tiers.
Actions sorted by effort (1 is about two minutes, 2 about an hour, 3 an
evening), exactly one at effort 1 and at most one at effort 3."""


def user_message(
    question: str,
    location: str = "",
    *,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    harder: bool = False,
    sources: str = "",
) -> str:
    """The question, the place, the search results, and the retry nudge."""
    question = (question or "").strip()
    location = (location or "").strip()
    if location and lat is not None and lon is not None:
        place = f"{location} (pin dropped at {lat:.5f}, {lon:.5f})"
    elif location:
        place = location
    elif lat is not None and lon is not None:
        place = f"the point at latitude {lat:.5f}, longitude {lon:.5f}"
    else:
        place = ("not given -- infer it from the question, or answer at the "
                 "national level and say so in the summary")

    msg = f'Question: "{question}"\nLocation: {place}.'
    if harder:
        msg += (
            "\n\nThe first pass came back thin. Look harder at tiers A and B: "
            "name the agency, the statistical series, the bill or docket "
            "number, the study and its authors. Then answer."
        )
    if sources:
        msg += f"\n\nSearch results you may use:\n\n{sources}"
    return msg


# ---------------------------------------------------------------------------
# Parsing the response
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def extract_json(text: str) -> dict:
    """Strip fences, take the first `{` to the last `}`, parse.

    Models wrap JSON in prose more often than they should; this is the
    forgiving reader, not a licence for the prompt to be vague.
    """
    cleaned = _FENCE_RE.sub("", text or "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise CivicError("The model answered in prose, not JSON.",
                         raw=(text or "").strip(), code="not_json")
    try:
        payload = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise CivicError(f"The JSON did not parse: {exc.msg}.",
                         raw=(text or "").strip(), code="not_json") from exc
    if not isinstance(payload, dict):
        raise CivicError("The model returned JSON that is not an object.",
                         raw=(text or "").strip(), code="not_json")
    return payload


def response_text(content: Iterable[Any]) -> str:
    """Join the model's text blocks (search results arrive as other types)."""
    out = []
    for block in content or []:
        if _btype(block) == "text":
            out.append(_bget(block, "text") or "")
    return "\n".join(out).strip()


def search_urls(content: Iterable[Any]) -> set[str]:
    """Every URL the model actually saw, normalised for comparison.

    Two places carry them: the `web_search_tool_result` blocks (the search
    results themselves) and the citations attached to text blocks. Anything
    else in the answer is the model's memory, which is exactly what we refuse
    to publish as a source.
    """
    found: set[str] = set()

    def _swallow(url: Any) -> None:
        norm = normalise_url(url)
        if norm:
            found.add(norm)

    for block in content or []:
        btype = _btype(block)
        if btype == "web_search_tool_result":
            for result in _bget(block, "content") or []:
                _swallow(_bget(result, "url"))
        elif btype == "text":
            for citation in _bget(block, "citations") or []:
                _swallow(_bget(citation, "url"))
    return found


def normalise_url(url: Any) -> str:
    """Canonical form for grounding comparison, or "" if it isn't a web URL.

    Host case and a trailing slash are noise; the fragment is the model's
    addition often enough that keeping it would drop real citations.
    """
    if not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, parts.query, ""))


def _btype(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def _bget(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


# ---------------------------------------------------------------------------
# Validation : the hierarchy, enforced
# ---------------------------------------------------------------------------

def _clean_str(value: Any, limit: int = 600) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _string_list(value: Any, limit: int, item_limit: int = 400) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _clean_str(item, item_limit)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def validate(payload: dict, allowed_urls: set[str]) -> tuple[dict, dict]:
    """Coerce the model's JSON into the Answer shape, dropping what isn't earned.

    Returns (answer, notes). `notes` counts what was dropped so the route can
    log validation failures without them being invisible in the UI.
    """
    notes = {"evidence_dropped": 0, "actions_dropped": 0, "mechanisms_dropped": 0,
             "tiers_corrected": 0}

    headline = _clean_str(payload.get("headline"), 240)
    if not headline:
        raise CivicError("The answer had no headline.",
                         raw=json.dumps(payload)[:4000], code="no_headline")

    mechanisms = []
    for item in payload.get("mechanisms") or []:
        if not isinstance(item, dict):
            notes["mechanisms_dropped"] += 1
            continue
        mechanism = _clean_str(item.get("mechanism"), 240)
        if not mechanism:
            notes["mechanisms_dropped"] += 1
            continue
        confidence = _clean_str(item.get("confidence"), 12).lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        mechanisms.append({
            "mechanism": mechanism,
            "confidence": confidence,
            "because": _clean_str(item.get("because"), 400),
        })
    dropped_over_cap = max(0, len(mechanisms) - MAX_MECHANISMS)
    notes["mechanisms_dropped"] += dropped_over_cap
    mechanisms = mechanisms[:MAX_MECHANISMS]

    # Evidence: no grounded URL, no item. This is the fabrication guard.
    evidence = []
    for item in payload.get("evidence") or []:
        if not isinstance(item, dict):
            notes["evidence_dropped"] += 1
            continue
        claimed_tier = _clean_str(item.get("tier"), 2).upper()
        claim = _clean_str(item.get("claim"), 500)
        raw_url = _clean_str(item.get("url"), 500)
        url = normalise_url(raw_url)
        if not claim or not url or url not in allowed_urls:
            notes["evidence_dropped"] += 1
            continue
        # The rung is a fact about the publisher, so read it off the host and
        # fall back to the model only where the host says nothing (most
        # journalism). This is what stops a Reddit thread being cited as
        # official data, and what promotes a census.gov page the model
        # mislabelled as news.
        tier = civic_sources.classify(
            raw_url, claimed_tier if claimed_tier in TIER_ORDER else "E")
        if tier != claimed_tier:
            notes["tiers_corrected"] += 1
        evidence.append({
            "tier": tier,
            "claim": claim,
            "source": _clean_str(item.get("source"), 120) or _host(url),
            "url": raw_url,
        })
        if len(evidence) >= MAX_EVIDENCE:
            break

    # Actions keep their text but must not carry a URL we never saw. An action
    # with no link at all is fine -- "email your council member" is the most
    # useful two-minute action there is, and it has no citation to check.
    actions = []
    for item in payload.get("actions") or []:
        if not isinstance(item, dict):
            notes["actions_dropped"] += 1
            continue
        what = _clean_str(item.get("what"), 240)
        if not what:
            notes["actions_dropped"] += 1
            continue
        try:
            effort = int(item.get("effort", 2))
        except (TypeError, ValueError):
            effort = 2
        effort = min(3, max(1, effort))
        raw_url = _clean_str(item.get("url"), 500)
        norm = normalise_url(raw_url)
        if raw_url and (not norm or norm not in allowed_urls):
            notes["actions_dropped"] += 1
            continue
        actions.append({
            "effort": effort,
            "what": what,
            "why": _clean_str(item.get("why"), 300),
            "url": raw_url,
        })
    actions.sort(key=lambda a: a["effort"])
    actions = _cap_effort_slots(actions, notes)

    answer = {
        "headline": headline,
        "summary": _clean_str(payload.get("summary"), 900),
        "mechanisms": mechanisms,
        "evidence": evidence,
        "disputed": _string_list(payload.get("disputed"), 8),
        "live_decisions": _string_list(payload.get("live_decisions"), 8),
        "actions": actions,
        "caveat": _clean_str(payload.get("caveat"), 300),
        "rewrites": _string_list(payload.get("rewrites"), MAX_REWRITES, 160),
        "tiers": sorted({e["tier"] for e in evidence}, key=TIER_ORDER.index),
    }
    return answer, notes


def _cap_effort_slots(actions: list[dict], notes: dict) -> list[dict]:
    """Exactly one two-minute action, at most one evening-long one.

    The promise on the page is "one thing you can actually do this week". Three
    competing two-minute asks is the failure mode that promise exists to avoid,
    so the extras are dropped rather than rendered.
    """
    kept, seen = [], {1: 0, 3: 0}
    for action in actions:
        effort = action["effort"]
        if effort in seen:
            if seen[effort] >= 1:
                notes["actions_dropped"] += 1
                continue
            seen[effort] += 1
        kept.append(action)
    return kept


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return "source"


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def _create(messages: list[dict], today: Optional[date] = None, *,
            retrieval: bool = False):
    """One Messages call. With `retrieval`, the search results are already in
    the prompt, so the model gets no tools and makes no extra round-trips."""
    kwargs = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": [{
            "type": "text",
            "text": system_prompt(today, retrieval=retrieval),
            # Identical on every query and long enough to be worth caching :
            # the retry pass reads it straight from cache.
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": messages,
    }
    if not retrieval:
        kwargs["tools"] = [_web_search_tool()]
    return _client().with_options(timeout=REQUEST_TIMEOUT_S).messages.create(**kwargs)


def synthesize(
    question: str,
    location: str = "",
    *,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    create=None,
    retrieve=None,
    today: Optional[date] = None,
) -> tuple[dict, dict]:
    """Answer one question. Returns (answer, meta).

    Two ways to find the sources, decided by whether EXA_API_KEY is set:
    we search the ladder ourselves in parallel and hand the results over
    (cheaper, faster, and the tiers are known before the model reads them),
    or the model searches for itself with Claude's web_search tool.

    Two things trigger the single retry: the model wrote prose instead of
    JSON, or the answer spans fewer than three tiers -- a thin search, not
    necessarily a thin world. Anything past that is honest failure: an empty
    rung is information, and pretending otherwise is what this product exists
    to avoid.
    """
    question = (question or "").strip()
    if not question:
        raise CivicError("Ask a question first.", code="empty_question")

    retrieving = retrieve is not None or civic_sources.available()
    retrieve = retrieve or civic_sources.gather
    if create is None:
        def create(messages, _retrieval=None):
            return _create(messages, today,
                           retrieval=retrieving if _retrieval is None else _retrieval)

    started = time.monotonic()
    attempts, last_error = [], None
    sources_found = 0

    for harder in (False, True):
        if harder and time.monotonic() - started > RETRY_DEADLINE_S:
            # The first pass was slow enough that a second would cost more
            # than the better answer is worth. Ship what we have.
            print("  [civic] first pass was slow; skipping the second search")
            break
        sources_block, allowed = "", set()
        if retrieving:
            results = retrieve(question, location, harder=harder)
            sources_found = max(sources_found, len(results))
            if not results and not harder:
                # Exa is configured but came back empty (bad key, quota, an
                # outage). Fall through to the model's own search rather than
                # answering with nothing to cite.
                print("  [civic] retrieval returned nothing; using web_search")
                retrieving = False
            else:
                sources_block = civic_sources.as_prompt_block(results)
                allowed = {u for u in (normalise_url(r["url"]) for r in results) if u}

        msg = user_message(question, location, lat=lat, lon=lon,
                           harder=harder, sources=sources_block)
        try:
            response = create([{"role": "user", "content": msg}])
        except CivicError:
            raise
        except Exception as exc:  # noqa: BLE001 : surface the cause, don't crash
            cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
            detail = f"{type(exc).__name__}: {exc}" + (f" (cause: {cause})" if cause else "")
            raise CivicError(f"The search could not be completed. {detail}",
                             code="upstream_error") from exc

        content = getattr(response, "content", None) or []
        # Whichever way the sources were found, a citation counts only if its
        # URL is in this set.
        allowed |= search_urls(content)
        try:
            answer, notes = validate(extract_json(response_text(content)), allowed)
        except CivicError as exc:
            last_error = exc
            if harder:
                # A second pass that came back unparseable does not erase a
                # usable first pass : a thin answer beats no answer.
                if attempts:
                    break
                raise
            continue

        notes["retried"] = harder
        attempts.append((answer, notes))

        # A vague question is answered, not retried : the rewrite suggestions
        # ARE the answer, and searching harder for "politics?" finds nothing.
        if answer["headline"] == VAGUE_HEADLINE:
            break
        if len(answer["tiers"]) >= MIN_TIERS:
            break

    if not attempts:
        raise last_error or CivicError("No answer could be produced.")

    # Keep whichever pass reached further up the ladder.
    answer, notes = max(attempts, key=lambda pair: (len(pair[0]["tiers"]),
                                                    len(pair[0]["evidence"])))
    notes["attempts"] = len(attempts)
    notes["sources"] = "exa" if retrieving else "web_search"
    notes["sources_found"] = sources_found
    notes["latency_ms"] = int((time.monotonic() - started) * 1000)
    return answer, notes


# ---------------------------------------------------------------------------
# Cache : sha256(question|location) -> answer, 24h
# ---------------------------------------------------------------------------

# In-process, not Redis: two Railway replicas means a permalink can miss on the
# replica that did not answer it, which costs one re-query and no correctness.
# Worth revisiting only when repeat traffic makes the miss rate matter.
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_MAX = 500


def cache_key(question: str, location: str = "") -> str:
    """The permalink id: sha256 of the normalised question + place."""
    q = " ".join((question or "").lower().split())
    loc = " ".join((location or "").lower().split())
    return hashlib.sha256(f"{q}|{loc}".encode()).hexdigest()[:16]


def cache_get(key: str, *, now: Optional[float] = None) -> Optional[dict]:
    now = time.time() if now is None else now
    hit = _CACHE.get(key)
    if not hit:
        return None
    stored_at, value = hit
    if now - stored_at > CACHE_TTL_S:
        _CACHE.pop(key, None)
        return None
    return value


def cache_put(key: str, value: dict, *, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    if len(_CACHE) >= _CACHE_MAX:
        # Cheap eviction: drop the oldest quarter rather than reaching for an
        # LRU. At 500 entries the scan is nothing and the churn is rare.
        for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:_CACHE_MAX // 4]:
            _CACHE.pop(stale, None)
    _CACHE[key] = (now, value)


def cache_clear() -> None:
    _CACHE.clear()
