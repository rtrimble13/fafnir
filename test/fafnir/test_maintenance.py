"""Tests for rolling-horizon maintenance (partitions + calendar)."""

from __future__ import annotations

import datetime as dt

import pytest

from fafnir.db import maintenance


def test_current_horizon_year():
    this_year = dt.date.today().year
    assert maintenance.current_horizon_year(0) == this_year
    assert maintenance.current_horizon_year(2) == this_year + 2


@pytest.mark.integration
def test_ensure_horizon_extends_partitions_and_calendar(db):
    # Far-future years with no partitions yet; repeatable via DROP.
    for y in (2033, 2034):
        db.execute(f"DROP TABLE IF EXISTS core.daily_price_y{y}")

    created, _ = maintenance.ensure_horizon(db, through_year=2034, floor_year=2033)
    assert created == 2
    for y in (2033, 2034):
        assert maintenance._table_exists(db, f"daily_price_y{y}")

    # The trading calendar was extended through 2034 (~250 trading days).
    n2034 = db.fetchval(
        "SELECT count(*) FROM ref.trading_calendar "
        "WHERE extract(year FROM trade_date) = 2034"
    )
    assert n2034 > 200

    # Idempotent: a second run creates nothing new.
    created2, cal2 = maintenance.ensure_horizon(db, through_year=2034, floor_year=2033)
    assert created2 == 0
    assert cal2 == 0

    for y in (2033, 2034):
        db.execute(f"DROP TABLE IF EXISTS core.daily_price_y{y}")


# ---------------------------------------------------------------------------
# refresh_marts (#96): a list of views, CONCURRENTLY decided before it runs, and a
# real failure never reported as success
# ---------------------------------------------------------------------------


@pytest.fixture()
def scratch_views(db):
    """Throwaway materialized views in mart, dropped afterwards:
    ``zz_unpopulated`` (WITH NO DATA, unique index), ``zz_no_index`` (populated, no
    unique index) and ``zz_fails`` (populated, unique index, and a division that
    fails once its divisor is zeroed)."""
    db.execute("CREATE TABLE mart.zz_divisor (n integer)")
    db.execute("INSERT INTO mart.zz_divisor VALUES (1)")
    db.execute(
        "CREATE MATERIALIZED VIEW mart.zz_unpopulated AS SELECT 1 AS id WITH NO DATA"
    )
    db.execute("CREATE UNIQUE INDEX ON mart.zz_unpopulated (id)")
    db.execute("CREATE MATERIALIZED VIEW mart.zz_no_index AS SELECT 1 AS id")
    db.execute(
        "CREATE MATERIALIZED VIEW mart.zz_fails AS SELECT 1 / n AS id FROM mart.zz_divisor"
    )
    db.execute("CREATE UNIQUE INDEX ON mart.zz_fails (id)")
    try:
        yield
    finally:
        for view in ("zz_unpopulated", "zz_no_index", "zz_fails"):
            db.execute(f"DROP MATERIALIZED VIEW IF EXISTS mart.{view}")
        db.execute("DROP TABLE IF EXISTS mart.zz_divisor")


@pytest.mark.integration
def test_refresh_marts_refreshes_the_list_concurrently_where_it_can(db):
    assert maintenance.MATVIEWS[0] == "mart.security_latest"
    assert maintenance.refresh_marts(db) == [("mart.security_latest", "concurrently")]
    assert maintenance.refresh_marts(db, concurrently=False) == [
        ("mart.security_latest", "plain")
    ]


@pytest.mark.integration
def test_an_unpopulated_view_is_refreshed_plainly_first(db, scratch_views):
    views = ["mart.zz_unpopulated"]
    assert maintenance.refresh_marts(db, views=views) == [
        ("mart.zz_unpopulated", "plain")
    ]
    assert maintenance.refresh_marts(db, views=views) == [
        ("mart.zz_unpopulated", "concurrently")
    ]


@pytest.mark.integration
def test_a_view_without_a_unique_index_falls_back_to_plain(db, scratch_views):
    assert maintenance.refresh_marts(db, views=["mart.zz_no_index"]) == [
        ("mart.zz_no_index", "plain")
    ]


@pytest.mark.integration
def test_a_real_failure_is_raised_and_the_others_still_refresh(db, scratch_views):
    db.execute("UPDATE mart.zz_divisor SET n = 0")
    with pytest.raises(maintenance.MartRefreshError) as caught:
        maintenance.refresh_marts(
            db, views=["mart.zz_fails", "mart.zz_unpopulated", "mart.zz_missing"]
        )
    failed = dict(caught.value.failures)
    assert set(failed) == {"mart.zz_fails", "mart.zz_missing"}
    assert "DivisionByZero" in failed["mart.zz_fails"]
    assert "no materialized view" in failed["mart.zz_missing"]
    # The view between them refreshed all the same.
    assert db.fetchval(
        "SELECT ispopulated FROM pg_matviews WHERE matviewname = 'zz_unpopulated'"
    )


@pytest.mark.integration
def test_in_a_transaction_a_fallback_and_a_failure_leave_it_usable(
    db, scratch_views, migrated_dsn
):
    """The bare ``except`` this replaced ran its fallback inside the transaction the
    failed attempt had aborted. Here the attempt is rolled back to a savepoint."""
    from fafnir.db.connection import Database

    db.execute("UPDATE mart.zz_divisor SET n = 0")
    with Database(migrated_dsn) as tx:
        assert tx.fetchval("SELECT 1") == 1  # a transaction is open
        assert maintenance.refresh_marts(tx, views=["mart.zz_no_index"]) == [
            ("mart.zz_no_index", "plain")
        ]
        with pytest.raises(maintenance.MartRefreshError):
            maintenance.refresh_marts(tx, views=["mart.zz_fails"])
        assert tx.fetchval("SELECT 2") == 2


@pytest.mark.integration
def test_the_command_names_what_it_refreshed_and_fails_on_a_failure(
    db, scratch_views, tmp_path, monkeypatch
):
    from click.testing import CliRunner

    from fafnir import cli

    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text("")
    run = CliRunner().invoke(cli.main, ["--config", str(rc), "db", "refresh-marts"])
    assert run.exit_code == 0, run.output
    assert "mart.security_latest: refreshed concurrently" in run.output
    assert "Marts refreshed." in run.output

    db.execute("UPDATE mart.zz_divisor SET n = 0")
    monkeypatch.setattr(
        maintenance, "MATVIEWS", ("mart.zz_fails", "mart.security_latest")
    )
    run = CliRunner().invoke(cli.main, ["--config", str(rc), "db", "refresh-marts"])
    assert run.exit_code != 0
    assert "Refreshing marts failed: mart.zz_fails: DivisionByZero" in run.output
