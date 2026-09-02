-- 0020_company_summary_marts.up.sql
-- Finish `mart` as duk's read seam, and add the relations a per-company summary
-- needs.
--
-- Two things land together because they are the same change seen from two sides.
--
-- 1. `mart` was never complete. ADR 0008 makes every per-person and per-agent role
--    a member of fafnir_app (SELECT on mart + ref, nothing else) and its
--    Consequences require that mart be the whole agent-visible world -- but
--    duk.datasource.db reads core.symbol_xref for symbol resolution and
--    core.daily_price for raw prices. Measured before this migration: as
--    fafnir_app, `duk -S db ph AAPL` fails with `permission denied for schema core`
--    at RESOLUTION, before it reaches either price relation, with and without
--    --adj. mart.v_symbol_lookup and mart.v_daily_price_raw close that.
--
-- 2. A company summary (`duk ls <ticker>`) needs profile, price-coverage,
--    corporate-action and data-quality facts per security. Those live in core and
--    ops; ops is granted to NO read role at all, fafnir_read included.
--
-- Both are answered the same way: a view in mart, owned by the migrator.
--
-- DEFINER RIGHTS ARE THE MECHANISM (ADR 0009). A view executes with its OWNER's
-- privileges unless created WITH (security_invoker = true). That is what lets a
-- mart-only role read a core- or ops-derived relation without ever being granted
-- core or ops. Do NOT set security_invoker on these views -- it would silently
-- restore the failure this migration exists to fix. The corollary is that adding a
-- mart view GRANTS every mart reader whatever it selects, so a new view here is a
-- deliberate act, not a convenience.
--
-- SELECT is not granted explicitly: ALTER DEFAULT PRIVILEGES IN SCHEMA mart (0001)
-- already grants it to fafnir_read and fafnir_app for objects the migrator creates,
-- which is how every existing mart object is reachable. The privilege test asserts
-- it rather than trusting it.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. mart.v_symbol_lookup -- the ticker -> security_id map.
--
-- A passthrough of core.symbol_xref, deliberately NOT a resolver. The resolution
-- LADDER (live ticker, then primary symbol, then a ticker the security used to
-- trade under) stays in duk.datasource.db / fafnir.db.repository, where it is
-- already stated once and copied once with a comment explaining why. Encoding the
-- precedence here as well would make three statements of one rule.
--
-- Step 2 of that ladder reads core.security by primary_symbol, not the xref, and
-- is served by mart.v_security_profile below -- which is why resolution reads two
-- views rather than one.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_symbol_lookup AS
SELECT
    x.symbol,
    x.security_id,
    x.is_primary,
    x.valid_from,
    x.valid_to
FROM core.symbol_xref x;

COMMENT ON VIEW mart.v_symbol_lookup IS
    'Ticker -> security_id over time (passthrough of core.symbol_xref). '
    'NULL valid_to = currently valid. The resolution ladder lives in the client.';

-- ---------------------------------------------------------------------------
-- 2. mart.v_daily_price_raw -- unadjusted OHLCV, as traded.
--
-- The raw counterpart to mart.v_daily_price_adjusted, so the unadjusted series has
-- a mart name too. The explicit name is the point: a client cannot read unadjusted
-- prices while believing they are adjusted (ADR 0001, ADR 0004).
--
-- Lineage columns (source, ingestion_run_id, loaded_at) are left out. They are
-- operational, not market data, and the seam should carry what a reader of prices
-- needs. Verified: partition pruning survives the view -- a security_id + date
-- range query plans byte-identically to the same query against core.daily_price
-- (one yearly partition scanned, not eleven).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_daily_price_raw AS
SELECT
    p.security_id,
    p.trade_date,
    p.open,
    p.high,
    p.low,
    p.close,
    p.volume,
    p.vwap
FROM core.daily_price p;

COMMENT ON VIEW mart.v_daily_price_raw IS
    'RAW (unadjusted) daily OHLCV as traded -- a split shows as a real jump. '
    'Use mart.v_daily_price_adjusted for the split/dividend-adjusted series.';

-- ---------------------------------------------------------------------------
-- 3. mart.v_security_profile -- descriptive facts, one row per security.
--
-- Not mart.security_latest: that MATERIALIZED view is the screening contract, is
-- refresh-lagged by design, and carries no identifiers, no listing dates and no
-- description. A profile lookup must be current, so this is an ordinary view.
--
-- `source` and `delisted_date` are here because step 2 of the resolution ladder
-- orders by them.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_security_profile AS
SELECT
    s.security_id,
    s.primary_symbol            AS symbol,
    s.company_name,
    s.asset_type,
    s.exchange_code,
    x.exchange_name,
    sec.sector_name,
    ind.industry_name,
    s.currency,
    s.country,
    s.cik,
    s.isin,
    s.cusip,
    s.is_actively_trading,
    s.is_etf,
    s.is_fund,
    s.ipo_date,
    s.delisted_date,
    s.market_cap_usd,
    s.beta,
    s.source,
    s.first_seen_at,
    s.updated_at,
    cp.description,
    cp.loaded_at                AS description_loaded_at
FROM core.security s
LEFT JOIN ref.exchange      x   ON x.exchange_code = s.exchange_code
LEFT JOIN ref.sector        sec ON sec.sector_id   = s.sector_id
LEFT JOIN ref.industry      ind ON ind.industry_id = s.industry_id
LEFT JOIN core.company_profile cp ON cp.security_id = s.security_id;

COMMENT ON VIEW mart.v_security_profile IS
    'Descriptive profile per security, read live. market_cap_usd and beta are a '
    'company-screener snapshot refreshed with the security master -- not history.';

-- ---------------------------------------------------------------------------
-- 4. mart.v_security_price_coverage -- what the warehouse HOLDS for a security.
--
-- Cheap aggregates only, grouped by security_id so a per-security filter pushes
-- down to the grouping key. Trailing returns, volatility and drawdown are
-- deliberately NOT here: they belong to the adjusted series and are computed in
-- duk from one bounded price pull, reusing duk.return_utils, where those formulas
-- already live and are already tested.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_security_price_coverage AS
SELECT
    p.security_id,
    MIN(p.trade_date)                                      AS first_trade_date,
    MAX(p.trade_date)                                      AS last_trade_date,
    COUNT(*)                                               AS bar_count,
    COUNT(DISTINCT EXTRACT(YEAR FROM p.trade_date))        AS distinct_years,
    (MAX(p.trade_date) - MIN(p.trade_date)) + 1            AS calendar_span_days,
    MIN(p.close)                                           AS min_close,
    MAX(p.close)                                           AS max_close,
    COUNT(*) FILTER (WHERE p.volume = 0)                   AS zero_volume_bars
FROM core.daily_price p
GROUP BY p.security_id;

COMMENT ON VIEW mart.v_security_price_coverage IS
    'Per-security raw price coverage: span, bar count, close range, zero-volume '
    'bars. Statistics on the ADJUSTED series are computed by the client.';

-- ---------------------------------------------------------------------------
-- 5. mart.v_security_action_summary -- corporate actions and adjustment state.
--
-- The trailing-12-month dividend sum is measured from the security's own latest
-- ex-date, not from now(): a view whose output changes with the clock cannot be
-- compared between two runs, and a delisted security would report a TTM of zero
-- rather than "nothing since it stopped trading". The client turns it into a yield
-- against the last close.
--
-- This view deliberately references NO numeric column of core.adjustment_factor --
-- only COUNT(*) and MAX(effective_date). A view that selected
-- cumulative_price_factor would become a second schema dependency on the two
-- columns 0013 had to re-type (and had to drop mart.v_daily_price_adjusted to do),
-- so every future change to them would mean dropping and recreating two views
-- instead of one. A client that wants the back-adjustment depth reads
-- mart.v_daily_price_adjusted.price_factor on the earliest bar, where the
-- dependency already exists and is already handled.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_security_action_summary AS
WITH actions AS (
    SELECT
        ca.security_id,
        COUNT(*) FILTER (WHERE ca.action_type = 'split')          AS split_count,
        MIN(ca.ex_date) FILTER (WHERE ca.action_type = 'split')   AS first_split_date,
        MAX(ca.ex_date) FILTER (WHERE ca.action_type = 'split')   AS last_split_date,
        COUNT(*) FILTER (WHERE ca.action_type = 'dividend')       AS dividend_count,
        MIN(ca.ex_date) FILTER (WHERE ca.action_type = 'dividend') AS first_dividend_date,
        MAX(ca.ex_date) FILTER (WHERE ca.action_type = 'dividend') AS last_dividend_date
    FROM core.corporate_action ca
    GROUP BY ca.security_id
),
factors AS (
    SELECT
        af.security_id,
        COUNT(*)                    AS adjustment_factor_rows,
        MAX(af.effective_date)      AS latest_factor_effective_date
    FROM core.adjustment_factor af
    GROUP BY af.security_id
)
SELECT
    a.security_id,
    a.split_count,
    a.first_split_date,
    a.last_split_date,
    ls.split_numerator          AS last_split_numerator,
    ls.split_denominator        AS last_split_denominator,
    a.dividend_count,
    a.first_dividend_date,
    a.last_dividend_date,
    ld.dividend_amount          AS last_dividend_amount,
    ld.currency                 AS last_dividend_currency,
    ttm.ttm_dividend_amount,
    COALESCE(f.adjustment_factor_rows, 0) AS adjustment_factor_rows,
    f.latest_factor_effective_date
FROM actions a
LEFT JOIN factors f ON f.security_id = a.security_id
LEFT JOIN LATERAL (
    SELECT c.split_numerator, c.split_denominator
    FROM core.corporate_action c
    WHERE c.security_id = a.security_id AND c.action_type = 'split'
    ORDER BY c.ex_date DESC LIMIT 1
) ls ON TRUE
LEFT JOIN LATERAL (
    SELECT c.dividend_amount, c.currency
    FROM core.corporate_action c
    WHERE c.security_id = a.security_id AND c.action_type = 'dividend'
    ORDER BY c.ex_date DESC LIMIT 1
) ld ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(c.dividend_amount) AS ttm_dividend_amount
    FROM core.corporate_action c
    WHERE c.security_id = a.security_id
      AND c.action_type = 'dividend'
      AND c.ex_date > a.last_dividend_date - 365
) ttm ON TRUE;

COMMENT ON VIEW mart.v_security_action_summary IS
    'Per-security split/dividend counts and boundaries plus adjustment-factor '
    'state. ttm_dividend_amount is the 365 days ending at the security''s own '
    'latest ex-date, so the row does not change with the clock.';

-- ---------------------------------------------------------------------------
-- 6. mart.v_security_dq_open -- the ops window (ADR 0009).
--
-- One row per OPEN flag. Deliberately not pre-aggregated: naming the offending bar
-- is what turns "something is wrong in this series" into "distrust the 2026-07-14
-- bar", and the client can group.
--
-- record_key is IN. It holds only {"trade_date"}, {"symbol","date"}, {"ex_date"},
-- {"last_date"}, {"effective_date"} -- dates and tickers, every one derived from
-- data a mart reader already reads in full via v_daily_price_raw.
--
-- detail is OUT. Mostly derived values too ({"move": ...}, {"prior_close": ...}),
-- but `adjustment_failed` writes {"error": "<ExcType>: <message>"} -- a raw Python
-- exception string that can carry psycopg internals, constraint names and paths.
-- That is the one DQ field not derived from readable market data. It stays behind
-- `fafnir dq list`, which runs as fafnir_ingest.
--
-- resolved_by / resolution_note (the human judgements, 0017) need no column
-- decision: ck_dq_flag_resolution_provenance forces both NULL whenever resolved_at
-- is, so `resolved_at IS NULL` already keeps human-written text off this seam.
-- THAT SAFETY IS IN THE WHERE CLAUSE, not the select list -- anyone relaxing this
-- view to show resolved flags takes it away.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_security_dq_open AS
SELECT
    f.dq_flag_id,
    f.security_id,
    f.check_name,
    f.severity,
    f.table_name,
    f.record_key,
    f.detected_at
FROM ops.data_quality_flag f
WHERE f.resolved_at IS NULL
  AND f.security_id IS NOT NULL;

COMMENT ON VIEW mart.v_security_dq_open IS
    'Open data-quality flags per security: counts, keys and dates, never detail. '
    'The open-only filter is what keeps resolution notes off this seam.';

COMMIT;
