"""
civic_geo.py : the stack of governments standing over one point.

Every address in the United States sits inside seven or eight separate
governments at once, each with its own election, its own money and its own
powers. Residents experience them as one blur -- "the city", "the government"
-- which is exactly why nobody knows that the body raising their water rate is
elected in an off-cycle race nobody covers.

This module names the stack for a point, and says what each layer actually
decides. The map uses it as a lens: pick a layer, see the boundary you are
inside, and ask that layer's question in that layer's vocabulary.

Two keyless sources:

  * the US Census Geocoder, which answers "which congressional district,
    state legislative districts, county, place and school districts contain
    this point" in one call, and
  * OpenStreetMap's is_in, which adds the council district (a `political`
    boundary the Census does not carry) and how the land at that point is
    actually used -- plus the relation ids the map needs to draw an outline.

What is deliberately NOT here: the legal zoning code. OpenStreetMap knows how
land is used, not what an ordinance permits, and pretending otherwise would be
the same unsourced confidence the rest of this surface refuses. The layer names
the authority and the question asks it for the code.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
TIMEOUT_S = 12.0
CACHE_TTL_S = 900

USER_AGENT = "surplus-civic/1.0 (+https://event.surpluslayer.com/civic)"


# ---------------------------------------------------------------------------
# What each layer decides. This is the part a resident cannot look up.
# ---------------------------------------------------------------------------

LAYERS: dict = {
    "congress": {
        "label": "U.S. House district",
        "color": "#C77FD0",
        "elected": "Every 2 years",
        "decides": [
            "Federal tax law, including the mortgage-interest and SALT deductions",
            "Housing vouchers and public-housing money",
            "Medicare, and the federal share of Medicaid",
            "The formulas that send highway, transit and broadband money to your state",
        ],
        "asks": "What has {name} voted on lately that reaches {place} — housing, "
                "tax, energy or transport — and what is moving in Congress now?",
    },
    "state_upper": {
        "label": "State Senate district",
        "color": "#7FB0D0",
        "elected": "Usually every 4 years",
        "decides": [
            "Whether cities may cap rents at all — state preemption settles that",
            "What a city is allowed to refuse to permit (zoning preemption)",
            "The school funding formula, and the property-tax rules underneath it",
            "Utility regulation: who pays for grid upgrades and how rates are set",
        ],
        "asks": "What bills in {name} would change rents, property tax, schools or "
                "utility bills in {place}, and where are they now?",
    },
    "state_lower": {
        "label": "State House district",
        "color": "#8FBF9F",
        "elected": "Usually every 2 years",
        "decides": [
            "The same statutes as the upper chamber — both must pass a bill",
            "Budget line items that reach individual cities and districts",
            "Housing production mandates and their exemptions",
        ],
        "asks": "What bills in {name} would change housing, tax or schools in "
                "{place}, and what is on the floor now?",
    },
    "county": {
        "label": "County",
        "color": "#D0B87F",
        "elected": "Board of supervisors or commissioners, by district",
        "decides": [
            "Assessing your property and collecting the tax on it",
            "Courts, the jail, the sheriff and public health",
            "Zoning and permits outside city limits",
            "Recording deeds, and running the elections themselves",
        ],
        "asks": "What is changing in {name} about property assessment, tax rates, "
                "or services that reach {place}?",
    },
    "place": {
        "label": "City",
        "color": "#5FD3A0",
        "elected": "Mayor and council",
        "decides": [
            "The zoning code: what may be built, how tall, how many homes",
            "Building permits, inspections and code enforcement",
            "Police and fire budgets, streets, rubbish, parking",
            "Local business taxes, fees and the rent board where one exists",
        ],
        "asks": "What is {name} deciding now about zoning, housing, permits or "
                "local taxes, and what changed in the last two years?",
    },
    "council": {
        "label": "Council district",
        "color": "#EEA76B",
        "elected": "Your single seat on the council",
        "decides": [
            "Your one vote on every city decision above",
            "Usually the first hearing a project in your street must pass",
            "Discretionary money for the district itself",
        ],
        "asks": "What is on the agenda for {name} in {place} — projects, hearings, "
                "and votes affecting this neighbourhood?",
    },
    "school": {
        "label": "School district",
        "color": "#B9791A",
        "elected": "School board, often in off-cycle elections",
        "decides": [
            "School budgets, staffing and which schools close",
            "Attendance boundaries — which school an address feeds into",
            "Bond measures that arrive on your property-tax bill",
        ],
        "asks": "What is changing in {name} — budget, boundaries, closures, board "
                "policy — and what decisions are open?",
    },
    "landuse": {
        "label": "Land use here",
        "color": "#A0A8B0",
        "elected": "Set by the city's zoning code",
        "decides": [
            "How this land is used today, as mapped by OpenStreetMap",
            "The legal zoning code is the city's, and is not in this data",
        ],
        "asks": "What zoning applies at {place}, what does it permit, and what "
                "change to it is being proposed?",
    },
}

# The order the map offers them in: closest to a resident's daily life first.
LAYER_ORDER = ["landuse", "council", "place", "school", "county",
               "state_lower", "state_upper", "congress"]

# Census Geocoder names its geography collections in prose ; match loosely so a
# vintage change does not silently empty a layer.
_CENSUS_KEYS = {
    "congress": ("congressional district",),
    "state_upper": ("upper chamber",),
    "state_lower": ("lower chamber",),
    "county": ("counties",),
    "place": ("incorporated places", "census designated places"),
    "school": ("unified school districts", "secondary school districts",
               "elementary school districts"),
}

_CACHE: dict = {}


def _cached(key: str, produce):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    value = produce()
    if len(_CACHE) > 400:
        for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:120]:
            _CACHE.pop(stale, None)
    _CACHE[key] = (now, value)
    return value


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def census_geographies(lat: float, lon: float) -> dict:
    """Every Census geography containing this point, in one keyless call."""
    import httpx
    params = {"x": f"{lon:.6f}", "y": f"{lat:.6f}", "benchmark": "Public_AR_Current",
              "vintage": "Current_Current", "layers": "all", "format": "json"}
    with httpx.Client(timeout=TIMEOUT_S) as client:
        resp = client.get(CENSUS_URL, params=params,
                          headers={"user-agent": USER_AGENT, "accept": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return ((resp.json().get("result") or {}).get("geographies") or {})


def osm_areas(lat: float, lon: float) -> list[dict]:
    """Boundaries containing the point, plus the land under it.

    Carries relation ids, which is what lets the map outline a district
    rather than merely name it.
    """
    import httpx
    query = (f"[out:json][timeout:20];is_in({lat:.6f},{lon:.6f})->.a;"
             f"rel(pivot.a);out tags;"
             f"(way(around:25,{lat:.6f},{lon:.6f})[\"landuse\"];"
             f" way(around:25,{lat:.6f},{lon:.6f})[\"building\"];);out tags 3;")
    with httpx.Client(timeout=TIMEOUT_S) as client:
        resp = client.post(OVERPASS_URL, data={"data": query},
                           headers={"user-agent": USER_AGENT})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.json().get("elements") or []


def outline_by_name(name: str) -> dict:
    """Find a boundary by name and return its geometry.

    The Census names the districts nobody can name from memory but hands back
    no shape, so the map would know you are in Congressional District 12 and
    be unable to draw it. OpenStreetMap has most of these boundaries mapped ;
    this looks one up by name and draws that.
    """
    name = " ".join((name or "").split())[:120]
    if not name or '"' in name or "\\" in name:
        return {}
    import httpx
    query = (f'[out:json][timeout:25];rel["boundary"]["name"="{name}"];out ids 1;')
    with httpx.Client(timeout=TIMEOUT_S) as client:
        resp = client.post(OVERPASS_URL, data={"data": query},
                           headers={"user-agent": USER_AGENT})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    found = resp.json().get("elements") or []
    if not found:
        return {}
    return outline(found[0].get("id"))


def outline(relation_id: int) -> dict:
    """One boundary's geometry, as GeoJSON, for drawing on the map."""
    import httpx
    query = f"[out:json][timeout:25];rel({int(relation_id)});out geom;"
    with httpx.Client(timeout=TIMEOUT_S + 10) as client:
        resp = client.post(OVERPASS_URL, data={"data": query},
                           headers={"user-agent": USER_AGENT})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    rings = []
    for element in resp.json().get("elements") or []:
        for member in element.get("members") or []:
            if member.get("type") != "way" or member.get("role") == "inner":
                continue
            line = [[p["lon"], p["lat"]] for p in member.get("geometry") or []
                    if "lat" in p and "lon" in p]
            if len(line) > 1:
                rings.append(line)
    if not rings:
        return {}
    return {"type": "MultiLineString", "coordinates": rings}


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------

def _census_layers(geographies: dict) -> dict:
    found: dict = {}
    for collection, entries in (geographies or {}).items():
        low = collection.lower()
        for key, needles in _CENSUS_KEYS.items():
            if key in found or not any(n in low for n in needles):
                continue
            for entry in entries or []:
                name = (entry.get("NAME") or entry.get("BASENAME") or "").strip()
                if not name:
                    continue
                found[key] = {"name": name, "detail": collection,
                              "geoid": entry.get("GEOID") or "", "source": "census"}
                break
    return found


def _osm_layers(elements: list) -> dict:
    found: dict = {}
    for element in elements or []:
        tags = element.get("tags") or {}
        name = (tags.get("name") or "").strip()
        boundary = tags.get("boundary") or ""
        level = tags.get("admin_level") or ""

        if boundary == "political" and name and "council" not in found:
            found["council"] = {"name": name, "source": "openstreetmap",
                                "detail": (tags.get("political_division") or
                                           "political district").replace("_", " "),
                                "relation": element.get("id")}
        elif boundary == "administrative" and name:
            slot = {"8": "place", "6": "county", "4": "state"}.get(str(level))
            if slot and slot not in found:
                found[slot] = {"name": name, "source": "openstreetmap",
                               "detail": f"admin level {level}",
                               "relation": element.get("id")}
        elif tags.get("landuse") and "landuse" not in found:
            found["landuse"] = {
                "name": str(tags["landuse"]).replace("_", " ").title(),
                "detail": "as mapped in OpenStreetMap, not the legal zoning code",
                "source": "openstreetmap",
            }
    return found


def stack(lat: float, lon: float) -> dict:
    """Every government standing over this point, closest to home first.

    Both sources are asked at once and either may fail : a layer that no
    source could name is simply absent, which is honest. The Census carries
    the districts nobody can name from memory ; OpenStreetMap carries the
    council district and the relation ids the outline needs.
    """
    key = f"{lat:.5f},{lon:.5f}"

    def build():
        results: dict = {"sources": {}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            census_future = pool.submit(census_geographies, lat, lon)
            osm_future = pool.submit(osm_areas, lat, lon)
            try:
                census = _census_layers(census_future.result())
                results["sources"]["census"] = len(census)
            except Exception as exc:  # noqa: BLE001 : one source down is not an outage
                census, results["sources"]["census"] = {}, f"{type(exc).__name__}"
            try:
                osm = _osm_layers(osm_future.result())
                results["sources"]["openstreetmap"] = len(osm)
            except Exception as exc:  # noqa: BLE001
                osm, results["sources"]["openstreetmap"] = {}, f"{type(exc).__name__}"

        layers = []
        for layer_key in LAYER_ORDER:
            spec = LAYERS[layer_key]
            # The Census is authoritative for the districts it carries ; OSM
            # fills the council district and anything the Census missed.
            found = census.get(layer_key) or osm.get(layer_key)
            if not found:
                continue
            layers.append({
                "key": layer_key,
                "label": spec["label"],
                "color": spec["color"],
                "elected": spec["elected"],
                "decides": spec["decides"],
                "name": found["name"],
                "detail": found.get("detail", ""),
                "source": found.get("source", ""),
                "relation": found.get("relation"),
                "geoid": found.get("geoid", ""),
            })
        results["layers"] = layers
        return results

    return _cached(key, build)


def question_for(layer: dict, place: str) -> str:
    """The question this layer raises here, in this layer's own vocabulary."""
    spec = LAYERS.get(layer.get("key") or "", {})
    template = spec.get("asks") or "What is changing in {name} near {place}?"
    return template.format(name=layer.get("name") or spec.get("label", "this district"),
                           place=place or "this area")


def probe(lat: float = 37.8044, lon: float = -122.2712) -> dict:
    """Which of the two sources answer from this deploy's network."""
    report: dict = {}
    try:
        report["census"] = {"collections": len(census_geographies(lat, lon))}
    except Exception as exc:  # noqa: BLE001
        report["census"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    try:
        report["openstreetmap"] = {"elements": len(osm_areas(lat, lon))}
    except Exception as exc:  # noqa: BLE001
        report["openstreetmap"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return report
