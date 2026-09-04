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

import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
import zlib
from collections import deque
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

# The model backend/agents/llm.py has been running in production for months.
# If the configured model is not available to this account, every question
# fails with the same unhelpful message, so fall back to the known-good one
# once and remember it for the life of the process.
FALLBACK_MODEL = "claude-sonnet-4-6"
_model_override = ""


def active_model() -> str:
    return _model_override or MODEL

# Output tokens are the biggest slice of wall-clock -- each one is decoded
# serially and nothing renders until the JSON closes -- so the shape rules in
# the prompt keep the answer short. This is headroom, not a target: a JSON
# object cut off mid-key is unparseable, and an answer that took 30s to write
# and then failed to parse is the worst outcome available.
MAX_TOKENS = 4000

# Only used on the web_search fallback, and kept deliberately small. Every
# search there is a serial round-trip -- the model decides, the search runs,
# the results land in context, the model re-reads all of it and decides again
# -- so twelve of them is minutes. Retrieval (civic_sources) does the same
# breadth in parallel in seconds ; this path is now just the safety net for a
# deploy where every backend is down.
_DEFAULT_MAX_USES = 3

# One query is 15-25s of search + synthesis. The SDK default (10 minutes) is
# not a useful failure signal, and this surface shares its threadpool with the
# CRM : a call that has not come back in 75s is holding a thread someone else
# needs, so give up and say so.
REQUEST_TIMEOUT_S = 75.0

# Only search again if the first pass left time for it. Bounds one request at
# roughly RETRY_DEADLINE_S + REQUEST_TIMEOUT_S instead of twice the timeout.
RETRY_DEADLINE_S = 45.0

# Answers must span at least this many tiers or we search again, harder.
# How many paused turns we will resume before giving up. Four is generous:
# each one is a continuation of the same search loop, not a fresh question.
MAX_TURNS = 4

MIN_TIERS = 3

# What a second pass costs depends on how we search. With Exa it is one more
# model call ; on the web_search fallback it is another whole tool loop, which
# can double the wait, so the bar for paying that is higher.
MIN_TIERS_FALLBACK = 2

# Effort for the synthesis call (low | medium | high). Unset means "don't send
# it" : the pinned anthropic==0.42.0 rejects output_config as a named argument,
# so it rides in extra_body, and an unrecognised field is not worth the risk by
# default. Set CIVIC_EFFORT=low to trade some depth for a faster answer.
EFFORT = (os.environ.get("CIVIC_EFFORT") or "").strip().lower()

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


# Which process answered you. Production runs two replicas behind one URL, so
# every number below is one replica's -- without this you cannot tell "nothing
# has failed" from "you asked the other one".
BOOT_ID = uuid.uuid4().hex[:8]
BOOTED_AT = time.time()

# The last upstream failures on THIS process, newest last. Five is enough to
# see whether they are all the same failure (a misconfiguration) or different
# ones (something flaky), which is the only question worth asking here.
LAST_ERROR: dict = {}
RECENT_ERRORS: "deque[dict]" = deque(maxlen=5)


def _remember_failure(exc: Exception, *, mode: str) -> dict:
    detail = {
        "boot": BOOT_ID,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": type(exc).__name__,
        "message": str(exc)[:400],
        "status": getattr(exc, "status_code", None),
        "model": active_model(),
        "mode": mode,
    }
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause:
        detail["cause"] = f"{type(cause).__name__}: {str(cause)[:200]}"
    LAST_ERROR.clear()
    LAST_ERROR.update(detail)
    RECENT_ERRORS.append(detail)

    # One line, one format, every failure. stdout is the only store both
    # replicas already share (Railway aggregates it), so this is the shared
    # error log : grep the deploy logs for "[civic] error" and you have every
    # replica's failures in one place, boot id included.
    print(f"  [civic] error boot={BOOT_ID} type={detail['type']} "
          f"status={detail.get('status')} model={detail['model']} "
          f"mode={mode} msg={detail['message'][:200]!r}")
    _post_to_webhook(detail)
    return detail


# Optional second copy of the same line, for anyone who would rather not read
# deploy logs. Off unless CIVIC_ERROR_WEBHOOK is set ; fire-and-forget on its
# own thread, because an error path that can itself block or raise is worse
# than no error path at all.
def _post_to_webhook(detail: dict) -> None:
    url = (os.environ.get("CIVIC_ERROR_WEBHOOK") or "").strip()
    if not url.startswith("https://"):
        return

    def send():
        try:
            import httpx
            with httpx.Client(timeout=5.0) as client:
                client.post(url, json={"source": "civic", **detail})
        except Exception as exc:  # noqa: BLE001 : never let reporting fail a request
            print(f"  [civic] error webhook failed: {type(exc).__name__}: {exc}")

    threading.Thread(target=send, daemon=True, name="civic-error-webhook").start()


def _is_tool_problem(exc: Exception) -> bool:
    """Whether the failure is "this account cannot use the web_search tool".

    Worth telling apart, because it has a specific remedy (enable web search
    for the org, or set EXA_API_KEY) and because the tempting workaround --
    dropping the tool and letting the model answer from memory -- would
    produce exactly the unsourced confident answer this product exists to
    prevent. So we name it and stop.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status not in (400, 403, 404):
        return False
    return "web_search" in text or "tool" in text


def _is_model_problem(exc: Exception) -> bool:
    """Whether this failure reads like "that model is not available to you".

    Deliberately loose: the cost of a wrong guess is one extra call on the
    model the rest of the app already uses, and the cost of missing it is
    every question failing forever.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status in (400, 403, 404) and "model" in text:
        return True
    return "model_not_found" in text or "not_found_error" in text


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

What the answer is FOR -- the causal chain:
- `mechanisms` is the heart of it. Each one names the actual instrument -- a
  bill or ordinance number, a rate change, a reassessment, a court ruling, a
  funding formula -- and traces the path from that instrument to the thing the
  reader noticed. "The county reassessed in 2025 under Measure X, and new
  assessments reach tax bills about eighteen months later" beats "housing
  costs went up".
- Order them by how much of the effect each explains, strongest first, and say
  in `because` what would have to be true for you to be wrong.
- If a legislative record is among the sources, cite it. A bill, docket or
  agenda link is worth more than a news story about that bill, and it is the
  thing the reader can actually act on.

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

Shape, and keep to it -- every extra sentence is time the reader spends
watching a spinner: 3-5 mechanisms. 5-9 evidence items across at least three
tiers, one sentence each. At most 3 disputed items and 3 live decisions. Two
or three actions, sorted by effort (1 is about two minutes, 2 about an hour, 3
an evening), exactly one at effort 1 and at most one at effort 3."""


def user_message(
    question: str,
    location: str = "",
    *,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    harder: bool = False,
    sources: str = "",
    brief: bool = False,
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
    if brief:
        # A dropped pin is not a vague question ; it is a request for a
        # briefing about a place. Answering it with "ask something narrower"
        # is the one response that wastes the click entirely.
        msg += (
            "\n\nThis is a place briefing: the resident dropped a pin here and "
            "wants to know what is going on. Do NOT ask for a narrower "
            "question and do NOT use the vague-question headline. Report what "
            "is live in this place now -- the two or three biggest things "
            "changing, the decisions open, and what is driving them -- and put "
            "sharper follow-up questions in `rewrites`."
        )
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


def search_errors(content: Iterable[Any]) -> list[str]:
    """The error codes the web_search tool returned, if it returned any.

    Server-side tool failures do not raise: the call succeeds with HTTP 200
    and the `web_search_tool_result` block carries an error object instead of
    a list of results. Missed, that looks exactly like "the model chose not to
    search" -- and the model then writes a confident answer from memory with
    an empty ladder under it, which is the single worst thing this surface can
    do. So it is caught here and treated as the outage it is.
    """
    codes = []
    for block in content or []:
        if _btype(block) != "web_search_tool_result":
            continue
        payload = _bget(block, "content")
        if isinstance(payload, list):
            continue                    # a list of results is the healthy shape
        code = _bget(payload, "error_code") or _bget(payload, "type") or "unknown"
        codes.append(str(code))
    return codes


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

def _request_kwargs(messages: list[dict], today: Optional[date],
                    retrieval: bool) -> dict:
    kwargs = {
        "model": active_model(),
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
    if EFFORT in ("low", "medium", "high"):
        # extra_body, not a named argument : the pinned SDK does not know
        # output_config yet. Unset by default -- see EFFORT above.
        kwargs["extra_body"] = {"output_config": {"effort": EFFORT}}
    return kwargs


def _create(messages: list[dict], today: Optional[date] = None, *,
            retrieval: bool = False, on_event=None):
    """One Messages call. With `retrieval`, the search results are already in
    the prompt, so the model gets no tools and makes no extra round-trips.

    `on_event(name, **fields)` turns the call into a live feed: each search the
    model runs, the moment it starts writing, and the headline as soon as it
    has been written. The headline is the first key in the JSON, so it lands
    seconds into the write rather than at the end of it -- which is the whole
    difference between a page that is working and a page that looks stuck.
    """
    client = _client().with_options(timeout=REQUEST_TIMEOUT_S)
    kwargs = _request_kwargs(messages, today, retrieval)
    convo = list(messages)
    blocks: list = []
    state = {"searches": 0, "text": [], "announced": False}
    stop = None

    # A long server-tool search loop does not end the turn : the API returns
    # stop_reason "pause_turn" with the searches done and the answer not
    # written yet, and expects the assistant content back to carry on. Not
    # resuming is why a twelve-search question came back as prose with no JSON
    # in it -- there was no JSON yet.
    for _ in range(MAX_TURNS):
        kwargs["messages"] = convo
        response = _one_turn(client, kwargs, on_event, state)
        content = list(getattr(response, "content", None) or [])
        blocks.extend(content)
        stop = getattr(response, "stop_reason", None)
        if stop != "pause_turn" or not content:
            break
        convo = convo + [{"role": "assistant", "content": content}]
        if on_event:
            on_event("resuming")

    return _Turns(blocks, stop)


class _Turns:
    """One logical response, possibly assembled from several paused turns."""

    def __init__(self, content: list, stop_reason: Optional[str]):
        self.content = content
        self.stop_reason = stop_reason


def _one_turn(client, kwargs: dict, on_event, state: dict):
    """One Messages call, streamed when anyone is listening."""
    if on_event is None:
        return client.messages.create(**kwargs)

    with client.messages.stream(**kwargs) as stream:
        for event in stream:
            kind = getattr(event, "type", "")
            if kind == "content_block_start":
                block_type = getattr(getattr(event, "content_block", None), "type", "")
                if block_type == "server_tool_use":
                    state["searches"] += 1
                    on_event("searching", n=state["searches"])
                elif block_type == "text":
                    on_event("writing")
            elif kind == "content_block_delta" and not state["announced"]:
                delta = getattr(getattr(event, "delta", None), "text", "") or ""
                if delta:
                    state["text"].append(delta)
                    headline = _peek_headline("".join(state["text"]))
                    if headline:
                        state["announced"] = True
                        on_event("headline", text=headline)
        return stream.get_final_message()


_HEADLINE_RE = re.compile(r'"headline"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _peek_headline(partial: str) -> str:
    """The headline out of a half-written JSON object, or "" if it isn't
    finished yet. Used only to show something true while the rest is written."""
    match = _HEADLINE_RE.search(partial)
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return ""


def _enough_tiers(answer: dict, retrieving: bool) -> bool:
    """Whether this answer is good enough to stop, given what a retry costs."""
    floor = MIN_TIERS if retrieving else MIN_TIERS_FALLBACK
    return len(answer["tiers"]) >= floor


def _call_with_model_fallback(create, msg, retrieving: bool):
    """Make the call ; if the model itself is the problem, switch once.

    A wrong CIVIC_MODEL is invisible from the outside -- every question just
    says the search could not be completed -- so rather than fail forever on
    a model this account cannot use, drop to the one the rest of the app runs
    on and say so in the log.
    """
    global _model_override
    try:
        return create([{"role": "user", "content": msg}])
    except Exception as exc:  # noqa: BLE001
        if not _is_model_problem(exc) or active_model() == FALLBACK_MODEL:
            raise
        _remember_failure(exc, mode="exa" if retrieving else "web_search")
        print(f"  [civic] {active_model()} is not usable here "
              f"({type(exc).__name__}); falling back to {FALLBACK_MODEL} "
              f"for the life of this process")
        _model_override = FALLBACK_MODEL
        return create([{"role": "user", "content": msg}])


def synthesize(
    question: str,
    location: str = "",
    *,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    brief: bool = False,
    create=None,
    retrieve=None,
    today: Optional[date] = None,
    on_event=None,
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

    # `on_event(name, **fields)` is how the page hears what is happening. It
    # is optional and never load-bearing : a failed callback must not cost an
    # answer that is already paid for.
    def emit(name, **fields):
        if not on_event:
            return
        try:
            on_event(name, **fields)
        except Exception as exc:  # noqa: BLE001
            print(f"  [civic] progress callback failed: {type(exc).__name__}: {exc}")

    retrieving = retrieve is not None or civic_sources.available()
    retrieve = retrieve or civic_sources.gather
    if create is None:
        def create(messages, _retrieval=None):
            return _create(messages, today,
                           retrieval=retrieving if _retrieval is None else _retrieval,
                           on_event=emit if on_event else None)

    started = time.monotonic()
    attempts, last_error = [], None
    sources_found = 0

    for harder in (False, True):
        if harder and time.monotonic() - started > RETRY_DEADLINE_S:
            # The first pass was slow enough that a second would cost more
            # than the better answer is worth. Ship what we have.
            print("  [civic] first pass was slow; skipping the second search")
            break
        emit("searching_again" if harder else "started")
        sources_block, allowed = "", set()
        if retrieving:
            emit("retrieving")
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
                emit("retrieved", count=len(results),
                     tiers=sorted({r["tier"] for r in results}))

        msg = user_message(question, location, lat=lat, lon=lon,
                           harder=harder, sources=sources_block, brief=brief)
        try:
            response = _call_with_model_fallback(create, msg, retrieving)
        except CivicError:
            raise
        except Exception as exc:  # noqa: BLE001 : surface the cause, don't crash
            detail = _remember_failure(exc, mode="exa" if retrieving else "web_search")
            if _is_tool_problem(exc) and not retrieving:
                raise CivicError(
                    "There is nothing to search with: this Anthropic account "
                    "cannot use the web_search tool, and no EXA_API_KEY is "
                    "set. Enable web search for the account, or add an Exa "
                    "key, and the question will work.",
                    code="no_search_backend",
                    raw=f"{detail['type']} (status {detail.get('status')}) on "
                        f"replica {BOOT_ID}: {detail['message']}",
                ) from exc
            raise CivicError(
                "The search could not be completed.",
                code="upstream_error",
                # The technical cause goes in `raw`, where the page shows it
                # under "show what we found" instead of in the headline.
                raw=f"{detail['type']} (status {detail.get('status')}) calling "
                    f"{detail['model']} on replica {BOOT_ID}: {detail['message']}",
            ) from exc

        content = getattr(response, "content", None) or []
        stop = getattr(response, "stop_reason", None)
        tool_errors = search_errors(content)
        if tool_errors and not retrieving:
            # The model was handed a search tool that then failed. Everything
            # it writes from here is memory, so stop rather than render it.
            _remember_failure(
                RuntimeError(f"web_search returned {', '.join(sorted(set(tool_errors)))}"),
                mode="web_search")
            raise CivicError(
                "The search itself failed, so there is nothing to answer from. "
                "This account's web_search tool returned "
                f"'{tool_errors[0]}'. Set EXA_API_KEY, or enable web search for "
                "the account, and ask again.",
                code="no_search_backend",
                raw=f"web_search errors: {tool_errors}",
            )
        # Whichever way the sources were found, a citation counts only if its
        # URL is in this set.
        allowed |= search_urls(content)
        try:
            answer, notes = validate(extract_json(response_text(content)), allowed)
        except CivicError as exc:
            if exc.code == "not_json":
                # Name the real reason where we know it. "The model answered in
                # prose" is only true sometimes ; a cut-off answer and a turn
                # that never finished searching look identical from here.
                if stop == "max_tokens":
                    exc.args = ("The answer was cut off before it finished.",)
                    exc.code = "truncated"
                elif stop == "pause_turn":
                    exc.args = ("The search was still running when the answer "
                                "was due.",)
                    exc.code = "unfinished"
                print(f"  [civic] unparseable answer stop_reason={stop} "
                      f"blocks={len(content)}")
            last_error = exc
            if harder:
                # A second pass that came back unparseable does not erase a
                # usable first pass : a thin answer beats no answer.
                if attempts:
                    break
                raise
            continue

        notes["retried"] = harder
        notes["search_errors"] = tool_errors
        attempts.append((answer, notes))

        # A vague question is answered, not retried : the rewrite suggestions
        # ARE the answer, and searching harder for "politics?" finds nothing.
        if answer["headline"] == VAGUE_HEADLINE:
            break
        if _enough_tiers(answer, retrieving):
            break

    if not attempts:
        raise last_error or CivicError("No answer could be produced.")

    # An answer with an empty ladder is not a thin answer, it is an unsourced
    # one : every claim in it came from the model rather than from a document.
    # The whole product is the refusal to publish that, so it is a failure
    # here rather than a page of grey "nothing found at this level".
    if all(not a["evidence"] and a["headline"] != VAGUE_HEADLINE for a, _ in attempts):
        raise CivicError(
            "Nothing could be found for this question -- no official record, "
            "no research, no reporting. Rather than answer from memory, this "
            "is where the search stops.",
            code="no_evidence",
            raw=json.dumps({"sources": "exa" if retrieving else "web_search",
                            "found": sources_found,
                            "backends": dict(civic_sources.LAST_RUN)})[:2000],
        )

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


# ---------------------------------------------------------------------------
# Shareable links
# ---------------------------------------------------------------------------
#
# The answer cache is per process and production runs two replicas, so a
# permalink backed by a cache id lands on the replica that has it about half
# the time and says "expired" the rest -- seconds after the answer was made,
# and always after a deploy. Rather than reach for a database the surface
# deliberately does not have, the link carries the question: any replica can
# serve it from cache when it has one and rebuild it when it does not.

MAX_TOKEN_CHARS = 2000
_MAX_TOKEN_BYTES = 4096


def share_token(question: str, location: str = "", brief: bool = False) -> str:
    """A permalink id that describes the question it answers."""
    payload = {"q": (question or "").strip()[:500],
               "l": (location or "").strip()[:160]}
    if brief:
        payload["b"] = 1
    packed = zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 9)
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


def read_token(token: str) -> Optional[dict]:
    """The question inside a permalink, or None if it isn't one of ours.

    Everything here is attacker-supplied, so it is bounded twice: the encoded
    form before decoding, and the decompressed form during it -- a few hundred
    bytes of base64 must not be allowed to become a gigabyte of JSON.
    """
    token = (token or "").strip()
    if not token or len(token) > MAX_TOKEN_CHARS:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        stream = zlib.decompressobj()
        body = stream.decompress(raw, _MAX_TOKEN_BYTES)
        if stream.unconsumed_tail:
            return None                    # bigger than any real question
        payload = json.loads(body)
    except Exception:                      # noqa: BLE001 : a bad link is not an error
        return None
    if not isinstance(payload, dict):
        return None
    question = _clean_str(payload.get("q"), 500)
    if not question:
        return None
    return {"question": question,
            "location": _clean_str(payload.get("l"), 160),
            "brief": bool(payload.get("b"))}


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


def cache_size() -> int:
    return len(_CACHE)
