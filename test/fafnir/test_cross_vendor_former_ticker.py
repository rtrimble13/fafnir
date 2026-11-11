"""The vendor's ticker history read for a victim on a former ticker (no database).

`security merge --across-vendors` folds FMP's row left on a ticker the vendor renamed
the fed key away from (MICS into RIME). :func:`former_rename` finds that rename in
the key's events and :func:`entered_on` when the key took the victim's ticker, which
bound the ticker period the merge records on the survivor. Every key and ticker is
synthetic (ADR 0012 §8).
"""

from __future__ import annotations

from datetime import date

from fafnir.ingest import cross_vendor_merge as cvm


def _rename(day, old, new, filed):
    """One rename as the vendor files it: both events under the issuer's ticker at
    load time, the old and new tickers as each event's other party."""
    return [
        {
            "event_date": day,
            "action": "tickerchangefrom",
            "ticker": filed,
            "contra_ticker": old,
        },
        {
            "event_date": day,
            "action": "tickerchangeto",
            "ticker": filed,
            "contra_ticker": new,
        },
    ]


#: ZZANC -> ZZOLD -> ZZNEW, filed under ZZNEW (the SMDM -> MICS -> RIME shape).
CHAIN = _rename(date(2022, 5, 24), "ZZANC", "ZZOLD", "ZZNEW") + _rename(
    date(2024, 9, 9), "ZZOLD", "ZZNEW", "ZZNEW"
)


def test_the_rename_away_from_the_victims_ticker_is_found():
    assert cvm.former_rename(CHAIN, "ZZOLD") == (date(2024, 9, 9), "ZZOLD", "ZZNEW")
    assert cvm.former_rename(CHAIN, "ZZANC") == (date(2022, 5, 24), "ZZANC", "ZZOLD")


def test_a_ticker_the_key_never_left_has_no_former_rename():
    assert cvm.former_rename(CHAIN, "ZZNEW") is None
    assert cvm.former_rename(CHAIN, "ZZOTH") is None
    assert cvm.former_rename([], "ZZOLD") is None


def test_the_newest_rename_away_wins_on_a_ticker_used_twice():
    events = (
        _rename(date(2020, 1, 6), "ZZOLD", "ZZMID", "ZZNEW")
        + _rename(date(2021, 1, 4), "ZZMID", "ZZOLD", "ZZNEW")
        + _rename(date(2024, 9, 9), "ZZOLD", "ZZNEW", "ZZNEW")
    )
    assert cvm.former_rename(events, "ZZOLD")[0] == date(2024, 9, 9)


def test_a_share_class_is_matched_in_fafnirs_spelling():
    events = _rename(date(2024, 9, 9), "ZZOLD.B", "ZZNEW.B", "ZZNEW.B")
    assert cvm.former_rename(events, "ZZOLD-B") == (
        date(2024, 9, 9),
        "ZZOLD.B",
        "ZZNEW.B",
    )


def test_entered_on_is_the_rename_into_the_ticker_before_the_rename_away():
    assert cvm.entered_on(CHAIN, "ZZOLD", date(2024, 9, 9)) == date(2022, 5, 24)
    # Nothing names the key's first ticker: the caller falls back to the victim's
    # own period.
    assert cvm.entered_on(CHAIN, "ZZANC", date(2022, 5, 24)) is None
    # A rename into the ticker after the date does not bound an earlier period.
    assert cvm.entered_on(CHAIN, "ZZOLD", date(2022, 5, 24)) is None
