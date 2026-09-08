"""
Candidate sourcing: the constraints that have to survive future edits.

Two of these are tripwires rather than unit tests -- they exist to fail loudly
when someone re-adds the FEC as a data source, or starts importing the CRM into
what is meant to stay an extractable surface. Both are mistakes that look
entirely reasonable in a diff and are expensive to undo later.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from backend import campaigns_sources as src

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
CAMPAIGN_MODULES = ("campaigns_sources.py", "campaigns_score.py",
                    "campaigns_races.py", "campaigns_filings.py",
                    "campaigns_tabular.py", "campaigns_ca.py",
                    "campaigns_pa.py", "campaigns_oh.py",
                    "campaigns_az.py", "campaigns_states.py",
                    "campaigns_generic.py")


def rec(name="Pat Doe", **kw) -> src.CandidateRecord:
    base = dict(name=name, office="U.S. House", state="CA", district="12")
    base.update(kw)
    return src.CandidateRecord(**base)


# --------------------------------------------------------------------------
# Tripwire: the FEC stays out (52 U.S.C. 30111(a)(4) / 11 CFR 104.15)
# --------------------------------------------------------------------------

def test_no_fec_backend_is_registered():
    for registry in (src.INCUMBENT_BACKENDS, src.STATE_FILING_SOURCES):
        for name in registry:
            assert "fec" not in name.lower(), f"FEC backend registered: {name}"


def test_no_fec_endpoint_is_called():
    """Catches an adapter that quietly reaches for api.open.fec.gov."""
    source = (BACKEND / "campaigns_sources.py").read_text()
    for module in CAMPAIGN_MODULES:
        text = (BACKEND / module).read_text()
        for needle in ("open.fec.gov", "fec.gov/data", "docquery.fec.gov"):
            assert needle not in text, f"{module} reaches an FEC endpoint: {needle}"
    assert "30111" in source, "the statutory citation explaining why is gone"


def test_candidate_record_carries_no_fec_derived_field():
    """Ranking on a fundraising figure copied from a filing is FEC data used
    for a commercial purpose even when the number never leaves the database."""
    names = set(src.CandidateRecord.__dataclass_fields__)
    for banned in ("fundraising", "receipts", "disbursements", "cash_on_hand",
                   "fec_id", "committee_id", "total_raised", "contributions"):
        assert banned not in names, f"CandidateRecord grew a {banned!r} field"


# --------------------------------------------------------------------------
# Tripwire: this surface stays extractable
# --------------------------------------------------------------------------

RELATIONSHIP_SIDE = {
    "book", "relationships", "relationship_agent", "relationship_watch",
    "updates_engine", "updates_scheduler", "updates_watch", "drafting",
    "reply_agent", "capture_enrich", "resolver", "email_sync", "message_sink",
    "send_flow", "sender", "followup_scheduler", "outreach", "unipile",
    "providers", "matters", "venue", "referral", "billing_plans",
}


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            for part in (node.module or "").split("."):
                if part:
                    found.add(part)
            found.update(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("module", CAMPAIGN_MODULES)
def test_campaign_modules_do_not_import_the_crm(module):
    """The campaign surface is built to be lifted out into its own repo if it
    earns one. That stays true only while nothing couples it to the CRM, and
    'we'll keep it clean' is not a mechanism -- this is.
    """
    imported = _imported_modules(BACKEND / module)
    leaked = imported & RELATIONSHIP_SIDE
    assert not leaked, f"{module} imports the relationship side: {sorted(leaked)}"


@pytest.mark.parametrize("module", CAMPAIGN_MODULES)
def test_campaign_modules_have_no_db_or_app_dependency(module):
    """Pure policy + retrieval, like config.py and civic_sources.py. A DB
    import here is the first step to this surface not being liftable."""
    imported = _imported_modules(BACKEND / module)
    for banned in ("db", "models", "main", "fastapi", "sqlalchemy"):
        assert banned not in imported, f"{module} imports {banned}"


# --------------------------------------------------------------------------
# Identity and merging
# --------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Pat O'Doe", "Pat O Doe"),
    ("José García", "Jose Garcia"),
    ("  Pat   Doe  ", "Pat Doe"),
    ("PAT DOE", "pat doe"),
])
def test_identity_folds_the_same_person_together(a, b):
    assert rec(a).identity() == rec(b).identity()


def test_identity_keeps_different_people_apart():
    assert rec("Pat Doe").identity() != rec("Pat Roe").identity()
    assert rec(state="CA").identity() != rec(state="NY").identity()


def test_leading_zero_districts_match():
    assert rec(district="01").identity() == rec(district="1").identity()


def test_merge_prefers_the_filing_over_the_roster():
    roster = rec(found_by="govtrack", status="incumbent", source_url="https://g.example/1")
    filing = rec(found_by="filing:ca", status="qualified", source_url="https://sos.example/1")
    merged = src.merge([roster, filing])
    assert len(merged) == 1
    assert merged[0].status == "qualified"
    assert merged[0].found_by == "filing:ca"


def test_merge_prefers_a_contactable_record():
    bare = rec(found_by="govtrack")
    reachable = rec(found_by="govtrack", contact_email="team@example.org")
    merged = src.merge([bare, reachable])
    assert len(merged) == 1
    assert merged[0].contact_email == "team@example.org"


def test_merge_is_stable_and_sorted():
    records = [rec("Zoe Zed", district="3"), rec("Ann Ay", district="1")]
    assert [r.name for r in src.merge(records)] == ["Ann Ay", "Zoe Zed"]
    assert src.merge(records) == src.merge(list(reversed(records)))


# --------------------------------------------------------------------------
# gather(): failure isolation and honest diagnostics
# --------------------------------------------------------------------------

def test_gather_collects_from_an_injected_filing_source():
    src._RESULT_CACHE.clear()
    sources = {"CA": lambda state, office: [rec("Ada Challenger", found_by="filing:ca")]}
    found = src.gather("CA", filing_sources=sources, incumbent_backends={})
    assert [r.name for r in found] == ["Ada Challenger"]
    assert src.LAST_RUN["backends"] == {"filing:CA": 1}


def test_gather_skips_filing_sources_for_other_states():
    src._RESULT_CACHE.clear()
    sources = {"NY": lambda state, office: [rec("Not In California")]}
    assert src.gather("CA", filing_sources=sources, incumbent_backends={}) == []


def test_one_failing_backend_does_not_take_down_the_run():
    src._RESULT_CACHE.clear()

    def broken(state, office):
        raise RuntimeError("upstream changed shape")

    sources = {"CA": lambda state, office: [rec("Ada Challenger")], "*": broken}
    found = src.gather("CA", filing_sources=sources, incumbent_backends={})
    assert [r.name for r in found] == ["Ada Challenger"]
    assert "RuntimeError" in str(src.LAST_RUN["backends"]["filing:*"])


def test_rate_limit_is_recorded_as_weather_not_a_crash():
    src._RESULT_CACHE.clear()

    def limited(state, office):
        raise src.RateLimited("slow down")

    found = src.gather("CA", filing_sources={"CA": limited}, incumbent_backends={})
    assert found == []
    assert src.LAST_RUN["backends"]["filing:CA"] == "rate-limited"


def test_total_failure_is_distinguishable_from_nothing_found():
    """Both return [] and mean completely different things."""
    src._RESULT_CACHE.clear()

    def broken(state, office):
        raise RuntimeError("down")

    src.gather("CA", filing_sources={"CA": broken}, incumbent_backends={})
    failed = dict(src.LAST_RUN["backends"])

    src._RESULT_CACHE.clear()
    src.gather("CA", filing_sources={"CA": lambda s, o: []}, incumbent_backends={})
    empty = dict(src.LAST_RUN["backends"])

    assert failed != empty
    assert empty["filing:CA"] == 0


def test_gather_without_a_state_returns_nothing():
    assert src.gather("") == []


def test_gather_deduplicates_across_backends():
    src._RESULT_CACHE.clear()
    sources = {"CA": lambda s, o: [rec("Pat Doe", found_by="filing:ca")]}
    roster = {"govtrack": lambda s, o: [rec("Pat Doe", found_by="govtrack")]}
    found = src.gather("CA", filing_sources=sources, incumbent_backends=roster)
    assert len(found) == 1
    assert src.LAST_RUN["raw"] == 2 and src.LAST_RUN["merged"] == 1


# --------------------------------------------------------------------------
# Coverage honesty
# --------------------------------------------------------------------------

def test_filing_coverage_names_exactly_the_states_that_are_wired():
    """Incumbent rosters contain no challengers, so coverage has to come from
    the filing registry and has to be visible rather than inferred from a thin
    result set. California is wired; nothing else is yet."""
    coverage = src.filing_coverage()
    assert coverage["state_count"] == 50
    assert set(coverage["states_with_filing_source"]) >= {"CA", "PA", "OH", "AZ"}
    assert coverage["has_challenger_coverage"] is True
    assert "govtrack" in coverage["incumbent_backends"]


def test_an_adapter_that_fails_to_import_is_reported_not_swallowed():
    """A missing state and a broken state must not look the same."""
    src._adapters_loaded = False
    src.ADAPTER_LOAD_ERRORS.clear()
    original = src._STATE_ADAPTER_MODULES
    try:
        src._STATE_ADAPTER_MODULES = ("campaigns_does_not_exist",)
        src.load_state_adapters()
        assert "campaigns_does_not_exist" in src.ADAPTER_LOAD_ERRORS
        assert "adapter_load_errors" in src.filing_coverage()
    finally:
        src._STATE_ADAPTER_MODULES = original
        src.ADAPTER_LOAD_ERRORS.clear()
        src._adapters_loaded = False
        src.load_state_adapters()


def test_importing_sources_alone_populates_the_registry():
    """A caller that never imports campaigns_ca still gets California.

    Checked in a FRESH INTERPRETER rather than with importlib.reload: reload
    hands back a new module object, but campaigns_ca is already in sys.modules
    so its body never re-runs and never re-registers against the new object.
    That is an artefact of reload, not of the wiring, and testing it that way
    would assert something the production path never does.
    """
    import subprocess
    import sys

    probe = ("from backend import campaigns_sources as s; "
             "s.load_state_adapters(); "
             "print(sorted(s.STATE_FILING_SOURCES), s.ADAPTER_LOAD_ERRORS)")
    result = subprocess.run([sys.executable, "-c", probe],
                            cwd=str(BACKEND.parent), capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    for state in ("'CA'", "'OH'", "'PA'", "'AZ'"):
        assert state in result.stdout, result.stdout
    assert "{}" in result.stdout, f"an adapter failed to load: {result.stdout}"


def test_incumbent_records_are_labelled_as_seat_context():
    """Nothing downstream should mistake a sitting-member row for a 2026
    ballot listing."""
    import inspect
    source = inspect.getsource(src)
    assert source.count("not a ballot listing") >= 2
