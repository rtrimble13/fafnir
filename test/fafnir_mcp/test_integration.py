"""
Integration tests for the MCP surface (needs FAFNIR_TEST_DSN).

The headline assertion is ADR 0008 implementation note 3: *"an integration test
asserting `price_history` through MCP and through `duk -S db` return the identical
series."* That is not ceremony. The whole justification for reusing
``duk.datasource.db`` rather than writing fresh SQL is that an agent and a person
reading the same security must get the same numbers; if they can diverge, the
reuse bought nothing and a disagreement between a human's `duk ls` and an agent's
answer becomes unfalsifiable.

The rest covers what unit tests structurally cannot: that the read-only transaction
really refuses the writes the lexer deliberately does not try to catch, and that
the ops tools return the columns the mart seam withholds.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from duk.datasource import db as ds_db
from fafnir.db import repository as repo
from fafnir.ingest import adjustments
from fafnir_mcp import tools as T
from fafnir_mcp.errors import ToolError

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")

SYMBOL = "MCPT"


def _seed(db):
    """One security with a 2:1 split, so raw and adjusted genuinely differ.

    The split is the point: a series where the two agree proves nothing about
    whether the right relation was read.
    """
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=SYMBOL,
        company_name="MCP Test Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=SYMBOL)
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": dt.date(2023, 5, 30),
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "security_id": sid,
                "trade_date": dt.date(2023, 5, 31),
                "open": 100,
                "high": 103,
                "low": 98,
                "close": 101,
                "volume": 1500,
            },
            {
                "security_id": sid,
                "trade_date": dt.date(2023, 6, 2),
                "open": 50,
                "high": 51,
                "low": 49,
                "close": 50,
                "volume": 2000,
            },
        ],
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2023, 6, 2),
        split_numerator=2,
        split_denominator=1,
    )
    adjustments.compute_for_security(db, sid)
    return sid


# ---------------------------------------------------------------------------
# The parity requirement (ADR 0008 implementation note 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adjusted", [False, True])
def test_mcp_and_duk_return_the_identical_series(db, adjusted):
    """Same security, same explicit range, same numbers -- both series.

    An explicit date range is used on purpose. `limit` without an end_date
    deliberately diverges (the MCP tool anchors to the security's own last bar,
    duk anchors to today -- see price_history's comment), and that divergence is
    asserted separately below. Everything else must agree exactly.
    """
    _seed(db)
    start, end = "2023-05-30", "2023-06-02"

    frame = ds_db.price_history(
        dsn=DSN,
        symbol=SYMBOL,
        start_date=start,
        end_date=end,
        frequency="day",
        limit=None,
        fields=None,
        adjusted=adjusted,
    )
    result = T.price_history(
        dsn=DSN, symbol=SYMBOL, start_date=start, end_date=end, adjusted=adjusted
    )

    assert result["row_count"] == len(frame)
    assert result["truncated"] is False
    assert result["series"] == ("adjusted" if adjusted else "raw")

    for row, (index, expected) in zip(result["rows"], frame.iterrows()):
        assert row["date"] == index.date().isoformat()
        for column in ("open", "high", "low", "close"):
            assert float(row[column]) == pytest.approx(float(expected[column]))
        assert int(row["volume"]) == int(expected["volume"])


def test_raw_and_adjusted_actually_differ(db):
    """Guards the guard: parity over two identical series would prove nothing."""
    _seed(db)
    raw = T.price_history(dsn=DSN, symbol=SYMBOL, adjusted=False)
    adj = T.price_history(dsn=DSN, symbol=SYMBOL, adjusted=True)
    assert [r["close"] for r in raw["rows"]] != [r["close"] for r in adj["rows"]]


def test_dates_are_plain_iso_days_not_timestamps(db):
    """A bar is a session, not an instant. `2023-06-02`, never `...T00:00:00`."""
    _seed(db)
    for row in T.price_history(dsn=DSN, symbol=SYMBOL)["rows"]:
        assert "T" not in row["date"], row["date"]
        dt.date.fromisoformat(row["date"])


def test_limit_returns_the_most_recent_bars_of_a_stale_security(db):
    """The deliberate divergence from duk, and why it exists.

    Every bar here is from 2023, so duk's `-n` -- which anchors its window to
    today -- returns nothing. That is correct for a live API and useless for a
    warehouse: an agent triaging a `stale` flag or looking at a delisted security
    needs its FINAL bars, and "no rows" would read as "no data held".
    """
    _seed(db)
    result = T.price_history(dsn=DSN, symbol=SYMBOL, limit=2)
    assert result["row_count"] == 2
    assert [r["date"] for r in result["rows"]] == ["2023-05-31", "2023-06-02"]


# ---------------------------------------------------------------------------
# The read-only transaction: the write barrier a lexer cannot be
# ---------------------------------------------------------------------------

# Each of these begins with SELECT or WITH, so sqlguard passes them by design --
# it refuses to guess whether a SELECT writes. PostgreSQL decides, in the read-only
# transaction, which is the second of ADR 0010's two independent barriers.
WRITES_THAT_LOOK_LIKE_READS = [
    "SELECT security_id INTO mart.mcp_probe FROM core.security LIMIT 1",
    "WITH gone AS (DELETE FROM ops.data_quality_flag RETURNING 1) SELECT * FROM gone",
    "WITH added AS (INSERT INTO ops.ingestion_run (endpoint) VALUES ('x') "
    "RETURNING 1) SELECT * FROM added",
]


@pytest.mark.parametrize("sql", WRITES_THAT_LOOK_LIKE_READS)
def test_read_only_transaction_refuses_writes_the_lexer_allows(db, sql):
    with pytest.raises(ToolError) as exc:
        T.sql_read(dsn=DSN, sql=sql)
    assert "read-only" in str(exc.value).lower()


def test_sql_read_returns_columns_and_caps_rows(db):
    result = T.sql_read(dsn=DSN, sql="SELECT generate_series(1, 5000) AS n")
    assert result["columns"] == ["n"]
    assert result["truncated"] is True
    assert result["row_count"] == 500  # SQL_MAX_ROWS


def test_sql_read_returns_an_empty_result_cleanly(db):
    """Zero rows is an answer, not a failure.

    A query that matches nothing is the normal outcome of ruling a hypothesis out
    -- "are there other securities with a gap on this date?" answered no -- so it
    must come back as an empty result with truncated False, not as an error an
    agent has to interpret.
    """
    result = T.sql_read(dsn=DSN, sql="SELECT 1 AS n WHERE false")
    assert result["rows"] == []
    assert result["row_count"] == 0
    assert result["truncated"] is False
    assert result["columns"] == ["n"]


# ---------------------------------------------------------------------------
# The ops tools: the columns the mart seam withholds (ADR 0009 -> ADR 0010)
# ---------------------------------------------------------------------------


def test_dq_queue_carries_detail_which_the_mart_window_does_not(db):
    """The single reason the ops tier exists.

    `mart.v_security_dq_open` excludes `detail` by design, so an agent on the read
    profile can see THAT a bar is flagged but not the measured move that flagged
    it. Triage needs the number.
    """
    sid = _seed(db)
    repo.add_dq_flag(
        db,
        check_name="outlier",
        severity="warn",
        security_id=sid,
        table_name="core.daily_price",
        record_key={"trade_date": "2023-06-02"},
        detail={"move": 0.75, "prior_close": "101"},
    )

    ops_view = T.dq_queue(dsn=DSN, symbol=SYMBOL)
    assert ops_view["row_count"] == 1
    assert ops_view["rows"][0]["detail"]["move"] == 0.75

    mart_view = T.dq_summary(dsn=DSN, symbol=SYMBOL)
    assert "detail" not in mart_view["rows"][0]


def test_dq_queue_shows_resolution_provenance(db):
    """Who decided this before, and why -- the other thing the seam withholds.

    It is what stops an agent re-litigating a judgement a human already made, and
    what makes the agent's own resolutions auditable.
    """
    sid = _seed(db)
    repo.add_dq_flag(
        db,
        check_name="gap",
        severity="warn",
        security_id=sid,
        table_name="core.daily_price",
        record_key={"trade_date": "2023-06-01"},
    )
    # add_dq_flag returns None (every call inserts a row), so the id comes back
    # from the queue -- which is also how `fafnir dq resolve` gets it.
    flag_id = db.fetchval(
        "SELECT dq_flag_id FROM ops.data_quality_flag WHERE security_id = %s", (sid,)
    )
    repo.resolve_dq_flags(
        db,
        repo.DqFilter(flag_ids=[flag_id]),
        resolved_by="claude",
        note="claude: exchange holiday, no bar expected",
    )

    assert T.dq_queue(dsn=DSN, symbol=SYMBOL)["row_count"] == 0
    resolved = T.dq_queue(dsn=DSN, symbol=SYMBOL, state="resolved")
    assert resolved["row_count"] == 1
    assert resolved["rows"][0]["resolved_by"] == "claude"
    assert "holiday" in resolved["rows"][0]["resolution_note"]


def test_dq_queue_globs_a_check_family(db):
    """`price_*` works here as it does in `fafnir dq list`, so a person and an
    agent narrow the queue the same way."""
    sid = _seed(db)
    for check in ("price_non_positive_price", "price_scale_collapse", "gap"):
        repo.add_dq_flag(
            db,
            check_name=check,
            severity="warn",
            security_id=sid,
            table_name="core.daily_price",
            record_key={"trade_date": "2023-06-02"},
        )
    assert T.dq_queue(dsn=DSN, check_name="price_*")["row_count"] == 2
    assert T.dq_queue(dsn=DSN, check_name="gap")["row_count"] == 1


def test_dq_totals_separates_repeating_price_flags_from_distinct_conditions(db):
    """price_* flags repeat per detection; the rest are one row per condition.

    Mixing them makes one stuck symbol look like a spreading problem, which is the
    misreading `operations.md` warns about.
    """
    sid = _seed(db)
    for _ in range(3):
        repo.add_dq_flag(
            db,
            check_name="price_non_positive_price",
            severity="warn",
            security_id=sid,
            table_name="core.daily_price",
            record_key={"trade_date": "2023-06-02"},
        )
    repo.add_dq_flag(
        db,
        check_name="gap",
        severity="warn",
        security_id=sid,
        table_name="core.daily_price",
        record_key={"trade_date": "2023-06-01"},
    )
    totals = T.dq_totals(dsn=DSN)
    assert totals["repeating_price_flags"] == 3
    assert totals["distinct_condition_flags"] == 1


def test_landing_payload_requires_an_endpoint(db):
    """It returns one payload, never a scan -- the tool cannot express one."""
    with pytest.raises(ToolError) as exc:
        T.landing_payload(dsn=DSN, endpoint="")
    assert "endpoint is required" in str(exc.value)


def test_schema_state_lists_applied_migrations(db):
    result = T.schema_state(dsn=DSN)
    versions = [row["version"] for row in result["rows"]]
    assert "0001" in versions
    assert "0021" in versions, "the ops reader role migration should be applied"


def test_unknown_symbol_explains_the_resolution_ladder(db):
    """The error an agent will hit most, so it should teach rather than just deny."""
    with pytest.raises(ToolError) as exc:
        T.price_history(dsn=DSN, symbol="NOSUCHTICKER")
    message = str(exc.value)
    assert "NOSUCHTICKER" in message
    assert "used to trade under" in message


def test_security_profile_omits_the_price_series(db):
    """A profile lookup must not drag five years of bars into the context budget."""
    _seed(db)
    profile = T.security_profile(dsn=DSN, symbol=SYMBOL)
    assert profile["profile"]["symbol"] == SYMBOL
    assert "adjusted_prices" not in profile
    assert profile["coverage"]["bar_count"] == 3
