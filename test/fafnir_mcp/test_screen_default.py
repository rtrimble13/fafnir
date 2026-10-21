"""
`screen_securities` returns actively trading securities unless asked (#79).

The warehouse retains every delisted security, the dead issuers minted from
Sharadar among them; an agent that forgets a filter would otherwise screen several
times the market as it trades now. The tool passes its flag to duk's screener,
which applies it (test/duk/test_db_datasource_integration.py).
"""

from __future__ import annotations

import pandas as pd

from fafnir_mcp import tools as T


def _capture(monkeypatch):
    import duk.datasource.db as ds_db

    seen = []

    def screen(**kw):
        seen.append(kw)
        return pd.DataFrame([{"symbol": "ZZLIVE"}])

    monkeypatch.setattr(ds_db, "screen", screen)
    return seen


def test_the_default_excludes_delisted(monkeypatch):
    seen = _capture(monkeypatch)
    result = T.screen_securities(dsn="dbname=x")
    assert seen[0]["includeDelisted"] is False
    assert seen[0]["isActivelyTrading"] is None
    assert "include_delisted" in result["note"]


def test_include_delisted_is_passed_through(monkeypatch):
    seen = _capture(monkeypatch)
    T.screen_securities(dsn="dbname=x", include_delisted=True)
    assert seen[0]["includeDelisted"] is True
