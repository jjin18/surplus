"""
Tests for backend/triage/csv_parser.py.

Luma CSVs vary widely in column naming and custom-question content;
these pin the flexible-mapping behavior so a future Luma export rename
doesn't silently drop data.
"""
from __future__ import annotations
import pytest

from backend.triage.csv_parser import (
    parse_csv, _resolve_field, _normalize_header, _coerce_linkedin,
)


# ── header resolution ─────────────────────────────────────────────────

@pytest.mark.parametrize("header,expected", [
    ("name", "name"),
    ("Name", "name"),
    ("Full Name", "name"),
    ("Email", "email"),
    ("Email Address", "email"),
    ("Contact Email", "email"),
    ("LinkedIn", "linkedin_url"),
    ("LinkedIn URL", "linkedin_url"),
    ("LinkedIn Profile URL (optional)", "linkedin_url"),  # substring fallback
    ("Job Title", "role"),
    ("Title", "role"),
    ("Company", "company"),
    ("Company Name", "company"),
    ("Website", "website"),
    ("Random Custom Question", None),
])
def test_resolve_field(header, expected):
    assert _resolve_field(header) == expected


def test_normalize_header_strips_punctuation_and_case():
    assert _normalize_header("LinkedIn URL?") == "linkedin url"
    assert _normalize_header("Job_Title") == "job title"


# ── linkedin coercion ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("https://linkedin.com/in/daniel-wang", "https://linkedin.com/in/daniel-wang"),
    ("www.linkedin.com/in/daniel-wang", "https://www.linkedin.com/in/daniel-wang"),
    ("daniel-wang", "https://www.linkedin.com/in/daniel-wang"),
    ("DanielWang_123", "https://www.linkedin.com/in/DanielWang_123"),
    ("", ""),
])
def test_coerce_linkedin(raw, expected):
    assert _coerce_linkedin(raw) == expected


# ── full CSV parsing ──────────────────────────────────────────────────

def test_parses_canonical_columns():
    csv_text = (
        "Name,Email,Job Title,Company,LinkedIn URL,Website\n"
        "Maya Rodriguez,maya@lo91r.com,Staff Infra,Lo91r,https://linkedin.com/in/maya,https://lo91r.com\n"
    )
    rows = parse_csv(csv_text)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Maya Rodriguez"
    assert r["email"] == "maya@lo91r.com"
    assert r["role"] == "Staff Infra"
    assert r["company"] == "Lo91r"
    assert r["linkedin_url"] == "https://linkedin.com/in/maya"
    assert r["website"] == "https://lo91r.com"
    assert r["raw_application_data"] == {}


def test_unknown_columns_preserved_in_raw():
    """Custom Luma questions ('Do you use Stripe?') should land in
    raw_application_data so PR C's scorer can read them."""
    csv_text = (
        "Name,Email,Do you use Stripe?,Are you a creator?\n"
        "Maya,maya@x.com,yes for B2B SaaS payments,no\n"
    )
    rows = parse_csv(csv_text)
    r = rows[0]
    assert r["name"] == "Maya"
    assert r["raw_application_data"]["Do you use Stripe?"] == "yes for B2B SaaS payments"
    assert r["raw_application_data"]["Are you a creator?"] == "no"


def test_skips_empty_rows():
    """Rows with no name AND no email are clearly junk (separator rows,
    empty trailing rows from spreadsheet exports)."""
    csv_text = (
        "Name,Email,Company\n"
        "Real Person,real@x.com,Acme\n"
        ",,\n"
        ",,SomeCompany\n"          # no name no email even if company set
        "Another,another@x.com,B\n"
    )
    rows = parse_csv(csv_text)
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"Real Person", "Another"}


def test_handles_bom():
    """Luma sometimes ships exports with a UTF-8 BOM."""
    csv_bytes = b"\xef\xbb\xbfName,Email\nMaya,m@x.com\n"
    rows = parse_csv(csv_bytes)
    assert len(rows) == 1
    assert rows[0]["name"] == "Maya"


def test_first_nonempty_wins_when_two_headers_map_same_field():
    """If both 'Company' and 'Organization' columns exist, take the first
    non-empty one rather than letting the later column blank-overwrite."""
    csv_text = (
        "Name,Email,Company,Organization\n"
        "Maya,m@x.com,Lo91r,\n"      # first column wins
        "Theo,t@x.com,,FlyIO\n"      # second column used when first blank
    )
    rows = parse_csv(csv_text)
    assert rows[0]["company"] == "Lo91r"
    assert rows[1]["company"] == "FlyIO"


def test_linkedin_handle_in_csv_gets_coerced_to_url():
    csv_text = "Name,Email,LinkedIn\nMaya,m@x.com,maya-rodriguez\n"
    rows = parse_csv(csv_text)
    assert rows[0]["linkedin_url"] == "https://www.linkedin.com/in/maya-rodriguez"


def test_empty_csv_returns_empty_list():
    assert parse_csv("") == []
    assert parse_csv("Name,Email\n") == []


def test_strips_whitespace_from_values():
    csv_text = "Name,Email,Company\n  Maya  ,  m@x.com  ,  Lo91r  \n"
    rows = parse_csv(csv_text)
    assert rows[0]["name"] == "Maya"
    assert rows[0]["email"] == "m@x.com"
    assert rows[0]["company"] == "Lo91r"
