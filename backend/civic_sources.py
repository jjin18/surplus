"""
civic_sources.py : where Civic's evidence comes from, and why it is fast.

The obvious way to answer a civic question is to hand Claude a web_search tool
and let it look. That is what this surface falls back to, and it is slow for a
structural reason: every search is a serial round-trip -- the model decides,
the search runs, the results land in context, the model re-reads everything and
decides again. Twelve of those is minutes of wall-clock, and the context (and
the bill) grows with each one.

So we do the searching. Every backend below is queried AT ONCE, in parallel,
and the model gets one shot at a pre-fetched, pre-ranked, snippet-capped
reading list. Retrieval is a few seconds regardless of how many sources are
consulted, because breadth costs threads rather than round-trips.

Breadth is the point. Each backend maps onto a rung of the evidence ladder and
is queried the way that kind of source expects to be asked:

    A  Federal Register, data.gov catalogue          rules, notices, datasets
    B  OpenAlex, Crossref                            papers, with DOIs
    C  (from hosts: universities, institutes)
    E  GDELT                                         worldwide news, 100+ languages
    F  Hacker News, Reddit                           what people are arguing about
    *  Exa, when EXA_API_KEY is set                  neural search across all of it

None of them needs a key except Exa, so a deploy with only ANTHROPIC_API_KEY
still gets a wide search. Each one is failure-isolated: a backend that is
down, rate-limited or has changed its response shape contributes nothing and
says so in the log, and the answer is built from whatever did come back.

The tier is assigned from the URL by classify() before the model reads a word,
so a Reddit thread cannot be cited as official data no matter how confidently
it is written.
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import urlsplit

EXA_SEARCH_URL = "https://api.exa.ai/search"

# Per-search timeout. Everything runs at once, so this is roughly the
# wall-clock cost of the whole retrieval step, not the sum of its parts.
TIMEOUT_S = 20.0

# The keyless backends are someone else's free API : be a good citizen and
# be identifiable, and never wait on one long enough to hurt the answer.
BACKEND_TIMEOUT_S = 8.0
USER_AGENT = "surplus-civic/1.0 (+https://event.surpluslayer.com/civic)"
CONTACT = (os.environ.get("CIVIC_CONTACT_EMAIL") or "civic@surpluslayer.com").strip()

# How much of each page the model gets to read. Enough to carry a number and
# its sentence; not enough for one verbose page to crowd out five others.
SNIPPET_CHARS = 700

MAX_RESULTS = 30


def _api_key() -> str:
    # Stripped for the same reason as ANTHROPIC_API_KEY : a pasted dashboard
    # value carries a trailing newline that httpx rejects as a header.
    return (os.environ.get("EXA_API_KEY") or "").strip()


def available() -> bool:
    """True when retrieval can run here (and `civic.py` should prefer it)."""
    return bool(_api_key())


# ---------------------------------------------------------------------------
# What to search for, one query per rung
# ---------------------------------------------------------------------------

_SOCIAL_DOMAINS = ["reddit.com", "x.com", "twitter.com", "news.ycombinator.com",
                   "nextdoor.com", "quora.com", "bsky.app"]

# `category` values are Exa's own; the rest is plain neural search. Each entry
# is deliberately phrased the way the source type describes itself, because
# that is what a semantic index matches on.
TIER_PLAN: list[dict] = [
    {"tier": "A", "results": 6, "category": None,
     "query": "{q} {place} official government data, agency statistics or public records"},
    {"tier": "B", "results": 5, "category": "research paper",
     "query": "{q} peer-reviewed study or working paper on the causes and effects"},
    {"tier": "C", "results": 5, "category": None,
     "query": "{q} {place} policy analysis from a research institute, university or legislative office"},
    {"tier": "D", "results": 4, "category": None,
     "query": "{q} explained by a named expert, economist or professional association"},
    {"tier": "E", "results": 6, "category": "news",
     "query": "{q} {place} what is happening now, reporting and coverage"},
    {"tier": "F", "results": 4, "category": None, "domains": _SOCIAL_DOMAINS,
     "query": "{q} {place} what residents are saying and complaining about"},
]

# When the first pass comes back thin, this is what "search harder at tiers A
# and B" means in retrieval terms: more results where numbers come from.
_HARDER = {"A": 10, "B": 9}


# ---------------------------------------------------------------------------
# The keyless backends
# ---------------------------------------------------------------------------
#
# Each one takes (question, place) and returns result dicts. They are written
# to be tolerant, not clever: pull the fields under whichever key this API
# happens to use, skip anything without a URL, and never raise. A response
# shape that has moved on since this was written yields nothing and shows up
# in the probe (GET /api/civic/selftest?probe=1) rather than breaking an
# answer.

def _fetch(url: str, params: dict, timeout: float = BACKEND_TIMEOUT_S,
           accept: str = "application/json"):
    """One GET, with a single polite retry when the far end says slow down.

    Several of these APIs are free and busy -- GDELT in particular allows only
    a request every few seconds per IP, and we run two replicas -- so a 429 is
    an ordinary event rather than a fault. One short backoff catches most of
    them ; a second 429 is reported as rate_limited and the answer is built
    without that backend.
    """
    import httpx
    headers = {"accept": accept, "user-agent": USER_AGENT}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, params=params, headers=headers)
        if resp.status_code == 429:
            time.sleep(1.2)
            resp = client.get(url, params=params, headers=headers)
    if resp.status_code == 429:
        raise RateLimited("rate_limited (HTTP 429)")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
    return resp


class RateLimited(RuntimeError):
    """A free API asking us to come back later. Expected, not broken."""


def _get_json(url: str, params: dict, timeout: float = BACKEND_TIMEOUT_S) -> dict:
    return _fetch(url, params, timeout).json()


def _get_text(url: str, params: dict, timeout: float = BACKEND_TIMEOUT_S) -> str:
    return _fetch(url, params, timeout, accept="application/rss+xml, text/xml")\
        .text[:400_000]


# Results keep for a few minutes, per process. Repeated questions about the
# same place are the common case, and it takes the edge off the rate limits.
_CACHE_TTL_S = 300
_RESULT_CACHE: dict = {}


def _cached(key: str, produce: Callable[[], list]) -> list:
    now = time.time()
    hit = _RESULT_CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    found = produce()
    if len(_RESULT_CACHE) > 300:
        for stale in sorted(_RESULT_CACHE, key=lambda k: _RESULT_CACHE[k][0])[:100]:
            _RESULT_CACHE.pop(stale, None)
    _RESULT_CACHE[key] = (now, found)
    return found


def _clean(text: Optional[str], limit: int = SNIPPET_CHARS) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(text or "")).split())[:limit]


def _result(tier: str, backend: str, title, url, snippet="", published="") -> Optional[dict]:
    url = (url or "").strip()
    if not url.startswith("http"):
        return None
    return {
        "tier": classify(url, tier),
        "found_by": backend,
        "title": _clean(title, 200),
        "url": url,
        "host": host_of(url),
        "published": str(published or "")[:10],
        "snippet": _clean(snippet),
    }


def _openalex(question: str, place: str) -> list[dict]:
    """Papers, by relevance. Keyless ; the mailto is the polite pool."""
    data = _get_json("https://api.openalex.org/works",
                     {"search": question, "per-page": 5, "mailto": CONTACT})
    out = []
    for work in data.get("results") or []:
        primary = work.get("primary_location") or {}
        url = (primary.get("landing_page_url") or work.get("doi")
               or work.get("id") or "")
        venue = ((primary.get("source") or {}).get("display_name") or "")
        out.append(_result("B", "openalex", work.get("display_name"), url,
                           snippet=_abstract_of(work) or venue,
                           published=work.get("publication_date")))
    return [r for r in out if r]


def _abstract_of(work: dict) -> str:
    """OpenAlex ships abstracts as a word -> positions index. Rebuild it."""
    index = work.get("abstract_inverted_index")
    if not isinstance(index, dict):
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions or []:
            words.append((position, word))
    words.sort()
    return " ".join(w for _, w in words)


def _crossref(question: str, place: str) -> list[dict]:
    """Journal articles with DOIs : the strongest tier-B links there are."""
    data = _get_json("https://api.crossref.org/works",
                     {"query": question, "rows": 5, "select":
                      "title,URL,abstract,container-title,issued", "mailto": CONTACT})
    out = []
    for item in ((data.get("message") or {}).get("items") or []):
        title = (item.get("title") or [""])[0]
        venue = (item.get("container-title") or [""])[0]
        parts = ((item.get("issued") or {}).get("date-parts") or [[]])[0]
        published = "-".join(str(p).zfill(2) for p in parts[:3]) if parts else ""
        out.append(_result("B", "crossref", title, item.get("URL"),
                           snippet=item.get("abstract") or venue, published=published))
    return [r for r in out if r]


def _federal_register(question: str, place: str) -> list[dict]:
    """US federal rules, proposed rules and notices -- tier A, and often the
    thing that is actually happening to someone right now."""
    data = _get_json("https://www.federalregister.gov/api/v1/documents.json",
                     {"per_page": 5, "order": "relevance",
                      "conditions[term]": f"{question} {place}".strip()})
    out = []
    for doc in data.get("results") or []:
        agencies = ", ".join(a.get("name", "") for a in (doc.get("agencies") or []))
        out.append(_result("A", "federal_register", doc.get("title"),
                           doc.get("html_url"),
                           snippet=doc.get("abstract") or agencies,
                           published=doc.get("publication_date")))
    return [r for r in out if r]


def _data_gov(question: str, place: str) -> list[dict]:
    """The US open-data catalogue : the dataset behind the number."""
    data = _get_json("https://catalog.data.gov/api/3/action/package_search",
                     {"q": f"{question} {place}".strip(), "rows": 4})
    out = []
    for pkg in ((data.get("result") or {}).get("results") or []):
        slug = pkg.get("name") or ""
        org = (pkg.get("organization") or {}).get("title") or ""
        out.append(_result("A", "data_gov", pkg.get("title"),
                           f"https://catalog.data.gov/dataset/{slug}" if slug else "",
                           snippet=pkg.get("notes") or org))
    return [r for r in out if r]


def _gdelt(question: str, place: str) -> list[dict]:
    """Worldwide news, 100+ languages, updated every 15 minutes.

    This is what makes the search regional rather than American: GDELT indexes
    local outlets everywhere, so a question about a city in Kerala or Kraków
    finds the papers that actually cover it.
    """
    data = _get_json("https://api.gdeltproject.org/api/v2/doc/doc",
                     {"query": f"{question} {place}".strip()[:200],
                      "mode": "artlist", "format": "json", "maxrecords": 8,
                      "sort": "hybridrel", "timespan": "6months"})
    out = []
    for art in data.get("articles") or []:
        where = " · ".join(x for x in (art.get("sourcecountry"), art.get("domain")) if x)
        out.append(_result("E", "gdelt", art.get("title"), art.get("url"),
                           snippet=where, published=(art.get("seendate") or "")[:8]))
    return [r for r in out if r]


_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S | re.I)


def _tag(block: str, name: str) -> str:
    match = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S | re.I)
    if not match:
        return ""
    text = match.group(1)
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.S)
    return " ".join(text.split())


def _google_news(question: str, place: str) -> list[dict]:
    """Tier E, second opinion. GDELT is broader and multilingual but rate-limits
    hard ; this is the one that answers when it doesn't, so "what is happening
    now" does not rest on a single busy API.

    The feed is XML, read with bounded regex rather than an XML parser: it is
    a fixed, simple shape and an untrusted document should not get an entity
    expander pointed at it.
    """
    body = _get_text("https://news.google.com/rss/search",
                     {"q": f"{question} {place}".strip(), "hl": "en-US",
                      "gl": "US", "ceid": "US:en"})
    out = []
    for block in _ITEM_RE.findall(body)[:8]:
        out.append(_result("E", "google_news", _tag(block, "title"),
                           _tag(block, "link"),
                           snippet=_tag(block, "source"),
                           published=_tag(block, "pubDate")[:16]))
    return [r for r in out if r]


def _hacker_news(question: str, place: str) -> list[dict]:
    """Tier F. Never evidence of a claim -- evidence that people are arguing."""
    data = _get_json("https://hn.algolia.com/api/v1/search",
                     {"query": question, "tags": "story", "hitsPerPage": 4})
    out = []
    for hit in data.get("hits") or []:
        url = hit.get("url") or (f"https://news.ycombinator.com/item?id={hit['objectID']}"
                                 if hit.get("objectID") else "")
        out.append(_result("F", "hacker_news", hit.get("title") or hit.get("story_title"),
                           url, snippet=hit.get("story_text") or "",
                           published=(hit.get("created_at") or "")[:10]))
    return [r for r in out if r]


def _reddit(question: str, place: str) -> list[dict]:
    """Tier F, and the one most likely to refuse us : Reddit rate-limits
    datacenter IPs hard. Treated like any other backend that can be down."""
    data = _get_json("https://www.reddit.com/search.json",
                     {"q": f"{question} {place}".strip(), "limit": 5,
                      "sort": "relevance", "t": "year"})
    out = []
    for child in ((data.get("data") or {}).get("children") or []):
        post = child.get("data") or {}
        permalink = post.get("permalink") or ""
        out.append(_result("F", "reddit", post.get("title"),
                           f"https://www.reddit.com{permalink}" if permalink else post.get("url"),
                           snippet=post.get("selftext") or f"r/{post.get('subreddit', '')}"))
    return [r for r in out if r]


def _govtrack(question: str, place: str) -> list[dict]:
    """US federal bills, by name and status. Keyless.

    A bill is the most useful tier-A link there is: it names the instrument,
    says where it has got to, and is the thing a resident can comment on.
    """
    data = _get_json("https://www.govtrack.us/api/v2/bill",
                     {"q": question, "sort": "-current_status_date", "limit": 4})
    out = []
    for bill in data.get("objects") or []:
        status = " ".join(str(bill.get("current_status") or "").split("_")).strip()
        number = bill.get("display_number") or ""
        out.append(_result("A", "govtrack", f"{number} — {bill.get('title', '')}".strip(" —"),
                           bill.get("link"),
                           snippet=f"Status: {status}. {bill.get('title_without_number', '')}",
                           published=bill.get("current_status_date")))
    return [r for r in out if r]


def _openstates(question: str, place: str) -> list[dict]:
    """Every US state legislature, which is where most of this actually
    happens. Needs a free OPENSTATES_API_KEY ; skipped without one."""
    key = (os.environ.get("OPENSTATES_API_KEY") or "").strip()
    if not key:
        return []
    params = {"q": question, "sort": "updated_desc", "per_page": 4}
    jurisdiction = _state_of(place)
    if jurisdiction:
        params["jurisdiction"] = jurisdiction
    import httpx
    with httpx.Client(timeout=BACKEND_TIMEOUT_S) as client:
        resp = client.get("https://v3.openstates.org/bills", params=params,
                          headers={"X-API-KEY": key, "accept": "application/json",
                                   "user-agent": USER_AGENT})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
    out = []
    for bill in resp.json().get("results") or []:
        where = ((bill.get("jurisdiction") or {}).get("name") or "")
        out.append(_result("A", "openstates",
                           f"{bill.get('identifier', '')} — {bill.get('title', '')}".strip(" —"),
                           bill.get("openstates_url"),
                           snippet=f"{where}. Last action {bill.get('latest_action_date', '?')}: "
                                   f"{bill.get('latest_action_description', '')}",
                           published=bill.get("latest_action_date")))
    return [r for r in out if r]


def _uk_parliament(question: str, place: str) -> list[dict]:
    """UK bills before Parliament. Keyless -- and the reason a question from
    Manchester gets a legislature link instead of an empty top rung."""
    data = _get_json("https://bills-api.parliament.uk/api/v1/Bills",
                     {"SearchTerm": question, "Take": 4, "SortOrder": "DateUpdatedDescending"})
    out = []
    for bill in data.get("items") or []:
        bill_id = bill.get("billId")
        out.append(_result("A", "uk_parliament", bill.get("shortTitle"),
                           f"https://bills.parliament.uk/bills/{bill_id}" if bill_id else "",
                           snippet=f"{bill.get('currentHouse', '')} · "
                                   f"{bill.get('currentStage', {}).get('description', '')}",
                           published=(bill.get("lastUpdate") or "")[:10]))
    return [r for r in out if r]


_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
}


def _state_of(place: str) -> str:
    """The US state named in a place string, for the state-legislature query.

    "Oakland, California, US" -> "California". Anything else -> "", which
    searches every jurisdiction rather than the wrong one.
    """
    for part in reversed([p.strip() for p in (place or "").split(",")]):
        if part.lower() in _US_STATES:
            return part.title()
    return ""


# name -> (function, needs_place). `needs_place` is false for the scholarly
# indexes: a causal study about rent control is not a local document, and
# pinning the query to a city is how you find nothing.
KEYLESS_BACKENDS: dict = {
    # Tier A first : the legislature, the rulemaking, the dataset.
    "govtrack": (_govtrack, False),
    "uk_parliament": (_uk_parliament, False),
    "openstates": (_openstates, True),
    "federal_register": (_federal_register, True),
    "data_gov": (_data_gov, True),
    "openalex": (_openalex, False),
    "crossref": (_crossref, False),
    "gdelt": (_gdelt, True),
    "google_news": (_google_news, True),
    "hacker_news": (_hacker_news, False),
    "reddit": (_reddit, True),
}


# ---------------------------------------------------------------------------
# Which rung a URL belongs on
# ---------------------------------------------------------------------------

_GOV_SUFFIXES = (".gov", ".mil", ".gov.uk", ".gc.ca", ".govt.nz", ".gov.au",
                 ".europa.eu", ".un.org", ".who.int", ".oecd.org", ".imf.org",
                 ".worldbank.org")

_RESEARCH_HOSTS = {
    "arxiv.org", "nber.org", "doi.org", "ssrn.com", "papers.ssrn.com",
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "sciencedirect.com",
    "springer.com", "link.springer.com", "jstor.org", "tandfonline.com",
    "onlinelibrary.wiley.com", "nature.com", "science.org", "aeaweb.org",
    "journals.sagepub.com", "academic.oup.com", "osf.io", "iza.org",
    "cambridge.org", "pnas.org", "plos.org", "econpapers.repec.org",
}

_INSTITUTE_HOSTS = {
    "brookings.edu", "urban.org", "rand.org", "lincolninst.edu",
    "taxfoundation.org", "epi.org", "cbpp.org", "mercatus.org", "itep.org",
    "manhattan-institute.org", "americanprogress.org", "aei.org",
    "resources.org", "rff.org", "jchs.harvard.edu", "terner.berkeley.edu",
    "pewresearch.org", "kff.org", "cato.org", "bipartisanpolicy.org",
}

_SOCIAL_HOSTS = set(_SOCIAL_DOMAINS) | {"facebook.com", "instagram.com",
                                        "tiktok.com", "threads.net",
                                        "mastodon.social", "medium.com"}


def host_of(url: str) -> str:
    """Registrable-ish host, lowercased, `www.` dropped. "" if not a web URL."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    host = parts.netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, known: set[str]) -> bool:
    """True when `host` is one of `known` or a subdomain of one."""
    return any(host == k or host.endswith("." + k) for k in known)


def is_social(url: str) -> bool:
    """Social media, forums, and personal blogging platforms : tier F, always.

    `civic.py` uses this to overrule the model. A Reddit thread cited as
    official data is the exact failure the ladder exists to prevent, and no
    amount of prompt wording makes that guarantee.
    """
    host = host_of(url)
    return bool(host) and _matches(host, _SOCIAL_HOSTS)


_LADDER = "ABCDEF"


def classify(url: str, claimed: str = "E") -> str:
    """The tier a URL earns from its publisher, given the tier it was claimed at.

    The job here is to stop a source claiming a stronger rung than it earns --
    a Reddit thread cited as official data, a think tank's paper cited as
    peer-reviewed. Publishers that settle the question (governments, journals,
    social platforms) set the tier outright; a university or institute caps it
    at C but is trusted when it claims something weaker. Everything else keeps
    the claimed tier, which for most journalism is E.
    """
    host = host_of(url)
    claimed = (claimed or "E").upper()
    if claimed not in _LADDER:
        claimed = "E"
    if not host:
        return claimed
    if _matches(host, _SOCIAL_HOSTS):
        return "F"
    if host.endswith(_GOV_SUFFIXES) or ".gov." in host:
        return "A"
    if _matches(host, _RESEARCH_HOSTS):
        return "B"
    if _matches(host, _INSTITUTE_HOSTS) or host.endswith((".edu", ".ac.uk")):
        return claimed if _LADDER.index(claimed) >= _LADDER.index("C") else "C"
    return claimed


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _post(body: dict) -> dict:
    import httpx
    headers = {"x-api-key": _api_key(), "content-type": "application/json",
               "accept": "application/json"}
    with httpx.Client(timeout=TIMEOUT_S) as client:
        resp = client.post(EXA_SEARCH_URL, headers=headers, json=body)
    if resp.status_code >= 400:
        # Raised, not swallowed : a dead key or an exhausted quota must show up
        # as a failed backend in LAST_RUN and the selftest, because silently
        # returning nothing looks identical to "the world has nothing".
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
    return resp.json()


def _one_search(step: dict, question: str, place: str, harder: bool) -> list[dict]:
    query = step["query"].format(q=question, place=place).replace("  ", " ").strip()
    body = {
        "query": query,
        "type": "auto",
        "numResults": _HARDER.get(step["tier"], step["results"]) if harder else step["results"],
        "contents": {"text": {"maxCharacters": SNIPPET_CHARS}},
    }
    if step.get("category"):
        body["category"] = step["category"]
    if step.get("domains"):
        body["includeDomains"] = step["domains"]

    # Let it raise : gather() records the failure per backend, which is how a
    # 402 (out of credits) becomes visible instead of looking like no results.
    data = _post(body)

    out = []
    for result in data.get("results") or []:
        url = (result.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        text = " ".join((result.get("text") or "").split())[:SNIPPET_CHARS]
        out.append({
            "tier": classify(url, step["tier"]),
            "found_by": step["tier"],
            "title": " ".join((result.get("title") or "").split())[:200],
            "url": url,
            "host": host_of(url),
            "published": (result.get("publishedDate") or "")[:10],
            "snippet": text,
        })
    return out


# What each backend returned on the last gather(), for the selftest and the
# logs : {"gdelt": 8, "reddit": "HTTP 429: ...", ...}. Per process, newest
# wins ; it answers "which half of the search is actually working".
LAST_RUN: dict = {}


def gather(question: str, location: str = "", *, harder: bool = False,
           search: Optional[Callable[[dict, str, str, bool], list[dict]]] = None,
           backends: Optional[dict] = None) -> list[dict]:
    """Ask every source at once and return what came back, strongest first.

    Breadth here costs threads, not seconds: the six Exa queries and the seven
    keyless backends all run concurrently, so consulting thirteen indexes takes
    about as long as consulting one. That is the whole argument for doing the
    searching ourselves instead of handing the model a search tool and waiting
    out its round-trips.

    An empty list is a legitimate outcome -- it means nothing was found, which
    the answer should say rather than invent around.
    """
    question = " ".join((question or "").split())[:300]
    place = " ".join((location or "").split())[:120] or ""
    if not question:
        return []

    run = search or _one_search
    backends = KEYLESS_BACKENDS if backends is None else backends
    jobs: list[tuple[str, Callable[[], list[dict]]]] = []

    # `search` injected means a test is driving the Exa path without a key.
    if available() or search is not None:
        for step in TIER_PLAN:
            jobs.append((f"exa:{step['tier']}",
                         lambda step=step: run(step, question, place, harder)))

    for name, (fn, needs_place) in backends.items():
        if name == "openstates" and not (os.environ.get("OPENSTATES_API_KEY") or "").strip():
            continue     # a free key nobody has set is not a failure to report
        jobs.append((name, lambda fn=fn, needs_place=needs_place:
                     fn(question, place if needs_place else "")))

    outcomes: dict = {}

    def attempt(job):
        name, call = job
        try:
            found = _cached(f"{name}|{question}|{place}|{harder}", call) or []
            outcomes[name] = len(found)
            return found
        except RateLimited:
            # Expected from a free API under load, and not worth alarming
            # about : the ladder is built from the other nine.
            outcomes[name] = "rate_limited"
            return []
        except Exception as exc:  # noqa: BLE001 : one backend down is not an outage
            outcomes[name] = f"{type(exc).__name__}: {str(exc)[:120]}"
            return []

    with ThreadPoolExecutor(max_workers=max(1, min(14, len(jobs)))) as pool:
        batches = list(pool.map(attempt, jobs))

    seen, results = set(), []
    for batch in batches:
        for item in batch or []:
            key = item["url"].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
    results.sort(key=lambda r: ("ABCDEF".index(r["tier"]) if r["tier"] in "ABCDEF" else 9))

    LAST_RUN.clear()
    LAST_RUN.update(outcomes)
    failed = {k: v for k, v in outcomes.items() if isinstance(v, str)}
    print(f"  [civic.sources] {len(results)} results from {len(jobs)} backends"
          + (f" ; failed: {failed}" if failed else ""))
    return results[:MAX_RESULTS]


def probe(question: str = "housing costs", place: str = "California") -> dict:
    """Run every backend once and report what each returned.

    The instrument for a deploy nobody can reach from a laptop: one request
    says which indexes answer, which are rate-limiting us, and which have
    changed shape since this was written.
    """
    report: dict = {"exa_key": available(), "backends": {}}
    for name, (fn, needs_place) in KEYLESS_BACKENDS.items():
        try:
            found = fn(question, place if needs_place else "") or []
            report["backends"][name] = {
                "results": len(found),
                "sample": found[0]["url"] if found else "",
                "tier": found[0]["tier"] if found else "",
            }
        except RateLimited:
            report["backends"][name] = {"rate_limited": True}
        except Exception as exc:  # noqa: BLE001
            report["backends"][name] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if available():
        try:
            hits = _one_search(TIER_PLAN[0], question, place, False)
            report["backends"]["exa"] = {"results": len(hits),
                                         "sample": hits[0]["url"] if hits else ""}
        except Exception as exc:  # noqa: BLE001
            report["backends"]["exa"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return report


def as_prompt_block(results: list[dict]) -> str:
    """The search results, written out for the model to read and cite.

    Numbered, tier-labelled, snippet-capped. The model may only cite URLs that
    appear here -- and `civic.validate()` enforces that afterwards, so this
    block is the whole world the answer is allowed to draw on.
    """
    if not results:
        return ("No search results came back. Say plainly that you could not "
                "find evidence for this question, and do not answer from memory.")
    lines = []
    for i, r in enumerate(results, 1):
        date = f" ({r['published']})" if r.get("published") else ""
        lines.append(
            f"[{i}] tier {r['tier']} | {r['host']}{date}\n"
            f"    title: {r['title']}\n"
            f"    url: {r['url']}\n"
            f"    text: {r['snippet']}"
        )
    return "\n".join(lines)
