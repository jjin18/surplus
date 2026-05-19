"""Feature-flag tests : default-off, env-var-on, route-gating behavior."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.curation import features


def test_default_state_is_all_off(monkeypatch):
    for name in features.NEAR_TERM_FEATURES:
        monkeypatch.delenv(f"SURPLUS_FEATURE_{name.upper()}", raising=False)
    snap = features.all_flags()
    assert set(snap.keys()) == set(features.NEAR_TERM_FEATURES)
    assert not any(snap.values())


@pytest.mark.parametrize("name", list(features.NEAR_TERM_FEATURES))
def test_env_var_flips_each_flag(name, monkeypatch):
    monkeypatch.setenv(f"SURPLUS_FEATURE_{name.upper()}", "1")
    assert features.is_enabled(name) is True


def test_truthy_values(monkeypatch):
    for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("SURPLUS_FEATURE_NEWS_SIGNAL", val)
        assert features.is_enabled("news_signal") is True
    for val in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv("SURPLUS_FEATURE_NEWS_SIGNAL", val)
        assert features.is_enabled("news_signal") is False


def test_unknown_feature_fails_closed():
    # Even setting the env var, an unknown feature stays off : prevents
    # typos in route handlers from accidentally enabling something.
    import os
    os.environ["SURPLUS_FEATURE_DOES_NOT_EXIST"] = "1"
    try:
        assert features.is_enabled("does_not_exist") is False
    finally:
        os.environ.pop("SURPLUS_FEATURE_DOES_NOT_EXIST", None)


def test_require_raises_404_when_off(monkeypatch):
    monkeypatch.delenv("SURPLUS_FEATURE_SPONSOR_MATCH", raising=False)
    with pytest.raises(HTTPException) as exc:
        features.require("sponsor_match")
    assert exc.value.status_code == 404


def test_require_passes_when_on(monkeypatch):
    monkeypatch.setenv("SURPLUS_FEATURE_SPONSOR_MATCH", "1")
    features.require("sponsor_match")  # should not raise
