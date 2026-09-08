"""
The tabular fetch layer: Socrata paging, delimited sniffing, and the failures
that must not be silent.

The paging guard is the one worth reading. A portal that ignores $offset hands
back page one forever, and without a page cap that is an infinite loop rather
than an error -- a hang in production is strictly worse than a raised
exception, because nothing reports it.
"""
from __future__ import annotations

import json

import pytest

from backend import campaigns_tabular as tab


def as_bytes(payload) -> bytes:
    return json.dumps(payload).encode()


# --------------------------------------------------------------------------
# Socrata URLs
# --------------------------------------------------------------------------

def test_resource_url_carries_paging():
    url = tab.socrata_url("data.example.gov", "abcd-1234", limit=100, offset=200)
    assert url.startswith("https://data.example.gov/resource/abcd-1234.json")
    assert "$limit=100" in url and "$offset=200" in url


def test_a_where_clause_is_percent_encoded():
    url = tab.socrata_url("d.gov", "a-1", where="office = 'Governor'")
    assert "$where=" in url
    assert " " not in url.split("$where=")[1]


# --------------------------------------------------------------------------
# Socrata paging
# --------------------------------------------------------------------------

def test_a_single_short_page_is_one_request():
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return as_bytes([{"name": "Ada"}, {"name": "Bo"}])

    rows = tab.fetch_socrata("d.gov", "a-1", fetcher=fetch)
    assert [r["name"] for r in rows] == ["Ada", "Bo"]
    assert len(calls) == 1


def test_a_full_page_is_followed_by_another_request():
    pages = [[{"n": i} for i in range(tab.SOCRATA_PAGE)], [{"n": "last"}]]
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return as_bytes(pages[len(calls) - 1])

    rows = tab.fetch_socrata("d.gov", "a-1", fetcher=fetch)
    assert len(rows) == tab.SOCRATA_PAGE + 1
    assert len(calls) == 2
    assert f"$offset={tab.SOCRATA_PAGE}" in calls[1]


def test_a_portal_that_ignores_offset_raises_instead_of_looping_forever():
    """A hang reports nothing; an exception reports itself."""
    full = as_bytes([{"n": i} for i in range(tab.SOCRATA_PAGE)])
    with pytest.raises(tab.SourceError, match="refusing to page forever"):
        tab.fetch_socrata("d.gov", "a-1", fetcher=lambda url: full, max_pages=3)


def test_an_html_error_page_is_not_mistaken_for_rows():
    with pytest.raises(tab.SourceError, match="did not return JSON"):
        tab.fetch_socrata("d.gov", "a-1", fetcher=lambda url: b"<html>502</html>")


def test_a_socrata_error_object_is_surfaced():
    payload = as_bytes({"error": True, "message": "Resource not found"})
    with pytest.raises(tab.SourceError, match="Resource not found"):
        tab.fetch_socrata("d.gov", "a-1", fetcher=lambda url: payload)


def test_a_json_scalar_is_not_a_table():
    with pytest.raises(tab.SourceError, match="not a list of rows"):
        tab.fetch_socrata("d.gov", "a-1", fetcher=lambda url: b'"nope"')


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def test_catalogue_search_returns_ids_and_names():
    payload = as_bytes({"results": [
        {"resource": {"id": "abcd-1234", "name": "2026 Candidates",
                      "updatedAt": "2026-08-30T00:00:00.000Z", "rows_count": 900},
         "link": "https://data.pa.gov/d/abcd-1234"},
        {"resource": {"id": "", "name": "no id"}, "link": ""},
    ]})
    hits = tab.discover_socrata("data.pa.gov", "candidate",
                                fetcher=lambda url: payload)
    assert len(hits) == 1                      # the id-less row is dropped
    assert hits[0]["id"] == "abcd-1234"
    assert hits[0]["updated"] == "2026-08-30"
    assert hits[0]["rows"] == 900


def test_catalogue_query_is_encoded_into_the_url():
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return as_bytes({"results": []})

    tab.discover_socrata("data.pa.gov", "candidate filings", fetcher=fetch)
    assert "domains=data.pa.gov" in seen[0]
    assert "candidate%20filings" in seen[0] or "candidate+filings" in seen[0]


# --------------------------------------------------------------------------
# Delimited files
# --------------------------------------------------------------------------

def test_csv_parses_into_dicts():
    rows = tab.parse_delimited(b"name,office\nAda,Governor\nBo,U.S. House\n")
    assert rows == [{"name": "Ada", "office": "Governor"},
                    {"name": "Bo", "office": "U.S. House"}]


def test_tab_separated_is_detected():
    rows = tab.parse_delimited("name\toffice\nAda\tGovernor\n")
    assert rows == [{"name": "Ada", "office": "Governor"}]


def test_semicolon_separated_is_detected():
    rows = tab.parse_delimited("name;office\nAda;Governor\n")
    assert rows == [{"name": "Ada", "office": "Governor"}]


def test_a_utf8_bom_does_not_end_up_in_the_first_column_name():
    """State exports are full of BOMs, and a '\\ufeffname' key silently fails
    every column lookup while looking completely normal in a log."""
    rows = tab.parse_delimited("﻿name,office\nAda,Governor\n".encode())
    assert list(rows[0]) == ["name", "office"]


def test_header_whitespace_is_stripped():
    rows = tab.parse_delimited("  name , office \nAda,Governor\n")
    assert rows[0]["name"] == "Ada" and rows[0]["office"] == "Governor"


def test_a_ragged_row_does_not_crash_the_parse():
    rows = tab.parse_delimited("name,office\nAda,Governor,extra,cells\n")
    assert rows[0]["name"] == "Ada"
    assert all(key is not None for key in rows[0])


def test_an_empty_file_is_no_rows_not_an_error():
    assert tab.parse_delimited(b"") == []
    assert tab.parse_delimited("   ") == []


def test_a_single_column_file_still_parses():
    rows = tab.parse_delimited("name\nAda\nBo\n")
    assert [r["name"] for r in rows] == ["Ada", "Bo"]


# --------------------------------------------------------------------------
# Format dispatch
# --------------------------------------------------------------------------

def test_a_csv_url_is_read_as_delimited():
    rows = tab.fetch_table("https://x.gov/list.csv",
                           fetcher=lambda url: b"name\nAda\n")
    assert rows == [{"name": "Ada"}]


def test_a_query_string_does_not_defeat_extension_dispatch():
    rows = tab.fetch_table("https://x.gov/list.csv?v=2",
                           fetcher=lambda url: b"name\nAda\n")
    assert rows == [{"name": "Ada"}]


def test_an_unlabelled_url_is_tried_as_delimited():
    rows = tab.fetch_table("https://x.gov/download",
                           fetcher=lambda url: b"name\nAda\n")
    assert rows == [{"name": "Ada"}]


def test_missing_openpyxl_is_a_clear_error(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_openpyxl(name, *args, **kw):
        if name == "openpyxl":
            raise ImportError("No module named 'openpyxl'")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", no_openpyxl)
    with pytest.raises(tab.SourceError, match="openpyxl is required"):
        tab.parse_xlsx(b"PK\x03\x04")
