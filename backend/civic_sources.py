"""
civic_sources.py : where Civic's evidence comes from.

Claude's own `web_search` tool can find these pages, but it searches on the
model's terms: one query at a time, full pages dropped into the context, and
an extra model round-trip per search. When `EXA_API_KEY` is set (the same key
the prospecting side uses) we do the searching ourselves instead:

  * six searches, one per rung of the evidence ladder, run in parallel --
    seconds instead of a serial tool loop;
  * a snippet cap per result, so the model reads a few thousand tokens of
    search results rather than whatever the pages happen to weigh;
  * a tier assigned from the URL itself, before the model sees it. A page on
    reddit.com is a social signal no matter how confidently it is written,
    and census.gov is official data even if the model forgets to say so.

Without the key, `available()` is False and `civic.py` falls back to Claude's
web_search tool. Same answers, more money and more waiting.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import urlsplit

EXA_SEARCH_URL = "https://api.exa.ai/search"

# Per-search timeout. Six run at once, so this is roughly the wall-clock cost
# of the whole retrieval step.
TIMEOUT_S = 20.0

# How much of each page the model gets to read. Enough to carry a number and
# its sentence; not enough for one verbose page to crowd out five others.
SNIPPET_CHARS = 700

MAX_RESULTS = 26


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
        # Loud, because a dead key silently degrades every answer on the site.
        print(f"  [civic.exa] search {resp.status_code}: {resp.text[:160]}")
        return {}
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

    try:
        data = _post(body)
    except Exception as exc:  # noqa: BLE001 : retrieval is best-effort
        print(f"  [civic.exa] tier {step['tier']} failed: {type(exc).__name__}: {exc}")
        return []

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


def gather(question: str, location: str = "", *, harder: bool = False,
           search: Optional[Callable[[dict, str, str, bool], list[dict]]] = None
           ) -> list[dict]:
    """Search every rung of the ladder at once and return what came back.

    Results are deduplicated by URL and ordered A -> F, so the model reads the
    strongest sources first. An empty list is a legitimate outcome: it means
    the ladder is empty for this question, which the answer should say.
    """
    question = " ".join((question or "").split())[:300]
    place = " ".join((location or "").split())[:120] or ""
    if not question:
        return []
    run = search or _one_search

    with ThreadPoolExecutor(max_workers=len(TIER_PLAN)) as pool:
        batches = list(pool.map(lambda step: run(step, question, place, harder), TIER_PLAN))

    seen, results = set(), []
    for batch in batches:
        for item in batch or []:
            key = item["url"].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
    results.sort(key=lambda r: ("ABCDEF".index(r["tier"]) if r["tier"] in "ABCDEF" else 9))
    return results[:MAX_RESULTS]


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
