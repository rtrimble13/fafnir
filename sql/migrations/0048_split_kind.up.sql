-- 0048_split_kind.up.sql
-- Say which split rows are spin factors (#283).
--
-- A spin-off is adjusted as one split row on its ex-date whose ratio is the inverse
-- of the total-return factor (spin-as-split, #282; doc/operations.md, "A spin-off is
-- a split row"). FMP files its spin-offs that way and the operator repairs add them
-- that way. Every reader of core.corporate_action took a split row as a change in
-- the share count, which is right for a split and wrong for a spin factor: across a
-- spin-off the market cap and the factor panel multiplied the last balance sheet's
-- count by the spin ratio, and overstated the cap by it until the next balance sheet
-- was known (VHI, 2007-03-27 to 05-07: 255.9M shares for 114.1M, about 2.24 times).
-- The shape of a ratio cannot tell the two apart (21:20 is a stock dividend), so the
-- rows are marked, not inferred.
--
--   core.corporate_action.split_kind
--       'shares' (a split: the count changes by the ratio) or 'spin_factor' (a
--       spin-off's price factor: no count changes). 'shares' for every row that
--       exists, and for a dividend. Written by `fafnir actions add --spin-factor`,
--       `actions kind` and `actions mark-spins`; never by a loader, so a vendor's
--       revision of a marked row's ratio keeps its kind.
--
--   ops.split_kind_change
--       One row per change of a row's kind: who, when, why, and on what evidence
--       (an operator's note, Sharadar's spinoff event). The kind is a column, so
--       every reader sees it; this is its record.
--
--   Readers. The price factors take both kinds, so adjusted prices are unchanged.
--   The share-count readers skip a spin factor: mart.fn_daily_market_cap,
--   mart.v_security_attributes and the factor panel (shares_outstanding_as_of, the
--   weighted shares, the dividends per share, and so market_cap, EV and every
--   yield). Each is its last definition (0042, 0043) with one line added, marked
--   `-- 0048`. mart.v_corporate_action gains split_kind as its last column, and
--   mart.v_security_action_summary counts share splits as splits, a spin factor
--   apart (spin_factor_count, last_spin_factor_date, appended). The adjustment
--   engine's volume factor skips a spin factor too (fafnir.ingest.adjustments): that
--   is a recompute, not this migration.
--
-- Nothing here changes a row's values: every row reads 'shares' until it is marked,
-- so until then every reader returns what it did. Mark with `fafnir actions
-- mark-spins` (a dry run lists what it would mark), then `fafnir db refresh-marts`.
--
-- Privileges: ops.split_kind_change takes 0001's and 0021's default privileges, as
-- every ops table. The views and functions keep theirs (CREATE OR REPLACE); the
-- grants are repeated below, as 0042 and 0043 did.

BEGIN;

-- ---------------------------------------------------------------------------
-- core.corporate_action.split_kind
-- ---------------------------------------------------------------------------
ALTER TABLE core.corporate_action
    ADD COLUMN split_kind TEXT NOT NULL DEFAULT 'shares',
    ADD CONSTRAINT ck_corp_action_split_kind
        CHECK (split_kind IN ('shares', 'spin_factor')),
    -- A dividend changes no count and is no spin factor.
    ADD CONSTRAINT ck_corp_action_spin_factor_split
        CHECK (split_kind = 'shares' OR action_type = 'split');

COMMENT ON COLUMN core.corporate_action.split_kind IS
    'shares: the split changes the share count by its ratio. spin_factor: a '
    'spin-off''s total-return factor stored as a split (spin-as-split, #282): the '
    'price factors read it, the share counts and adjusted volume do not (0048, '
    '#283). shares for a dividend. Set by `fafnir actions add --spin-factor`, '
    '`actions kind` and `actions mark-spins`, recorded in ops.split_kind_change; no '
    'loader writes it.';

-- ---------------------------------------------------------------------------
-- ops.split_kind_change
-- ---------------------------------------------------------------------------
CREATE TABLE ops.split_kind_change (
    change_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- No foreign key, as ops.operator_override: a merge or an operator delete must
    -- not be blocked by the record, nor take it with them.
    corporate_action_id BIGINT NOT NULL,
    security_id         BIGINT NOT NULL,
    ex_date             DATE   NOT NULL,
    split_numerator     NUMERIC(20, 6),
    split_denominator   NUMERIC(20, 6),
    source              TEXT   NOT NULL,
    old_kind            TEXT   NOT NULL,
    new_kind            TEXT   NOT NULL,
    basis               TEXT   NOT NULL,
    evidence            JSONB  NOT NULL DEFAULT '{}'::jsonb,
    note                TEXT   NOT NULL,
    created_by          TEXT   NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_split_kind_change_kinds CHECK (
        old_kind IN ('shares', 'spin_factor') AND new_kind IN ('shares', 'spin_factor')
        AND old_kind <> new_kind
    ),
    CONSTRAINT ck_split_kind_change_basis
        CHECK (basis IN ('operator', 'operator_note', 'sharadar_spinoff')),
    CONSTRAINT ck_split_kind_change_note CHECK (btrim(note) <> ''),
    CONSTRAINT ck_split_kind_change_by CHECK (btrim(created_by) <> '')
);

COMMENT ON TABLE ops.split_kind_change IS
    'Every change of a split row''s core.corporate_action.split_kind (0048, #283), '
    'with the row as it stood. Grain: change_id. basis: operator (by hand, `actions '
    'add --spin-factor` or `actions kind`), operator_note (`actions mark-spins`: the '
    'operator''s add names a spin-off), sharadar_spinoff (`actions mark-spins`: '
    'Sharadar''s ACTIONS has a spinoff of the security on the ex-date; evidence '
    'names the event).';

CREATE INDEX ix_split_kind_change_security
    ON ops.split_kind_change (security_id, ex_date);

-- ---------------------------------------------------------------------------
-- mart.v_corporate_action: split_kind
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_corporate_action AS
SELECT
    ca.corporate_action_id,
    ca.security_id,
    ca.action_type,
    ca.ex_date,
    ca.record_date,
    ca.payment_date,
    ca.declaration_date,
    ca.split_numerator,
    ca.split_denominator,
    ca.dividend_amount,
    ca.currency,
    ca.source,
    ca.split_kind                                        -- 0048
FROM core.corporate_action ca;

COMMENT ON VIEW mart.v_corporate_action IS
    'Split and cash-dividend events at (security_id, action_type, ex_date) grain. '
    'Passthrough of core.corporate_action without load lineage. split_kind says '
    'whether a split changes the share count (shares) or is a spin-off''s price '
    'factor (spin_factor, 0048).';

-- ---------------------------------------------------------------------------
-- mart.v_security_action_summary: a spin factor is not counted as a split
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_security_action_summary AS
WITH actions AS (
    SELECT
        ca.security_id,
        -- 0048: splits are share splits; spin factors are counted apart.
        COUNT(*) FILTER (WHERE ca.action_type = 'split' AND ca.split_kind = 'shares')
            AS split_count,
        MIN(ca.ex_date) FILTER (WHERE ca.action_type = 'split' AND ca.split_kind = 'shares')
            AS first_split_date,
        MAX(ca.ex_date) FILTER (WHERE ca.action_type = 'split' AND ca.split_kind = 'shares')
            AS last_split_date,
        COUNT(*) FILTER (WHERE ca.action_type = 'dividend')       AS dividend_count,
        MIN(ca.ex_date) FILTER (WHERE ca.action_type = 'dividend') AS first_dividend_date,
        MAX(ca.ex_date) FILTER (WHERE ca.action_type = 'dividend') AS last_dividend_date,
        COUNT(*) FILTER (WHERE ca.split_kind = 'spin_factor')     AS spin_factor_count,
        MAX(ca.ex_date) FILTER (WHERE ca.split_kind = 'spin_factor') AS last_spin_factor_date
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
    f.latest_factor_effective_date,
    a.spin_factor_count,                                 -- 0048
    a.last_spin_factor_date                              -- 0048
FROM actions a
LEFT JOIN factors f ON f.security_id = a.security_id
LEFT JOIN LATERAL (
    SELECT c.split_numerator, c.split_denominator
    FROM core.corporate_action c
    WHERE c.security_id = a.security_id AND c.action_type = 'split'
      AND c.split_kind = 'shares'                        -- 0048
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
    'latest ex-date, so the row does not change with the clock. The split columns '
    'are share splits; spin factors (split_kind, 0048) are spin_factor_count and '
    'last_spin_factor_date.';

-- ---------------------------------------------------------------------------
-- mart.fn_daily_market_cap: 0042's, the splits a count follows are share splits
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mart.fn_daily_market_cap(
    security_ids bigint[], start_date date DEFAULT NULL, end_date date DEFAULT NULL
)
RETURNS TABLE (
    security_id bigint,
    trade_date date,
    currency text,
    close_raw numeric,
    shares_outstanding numeric,
    share_factor numeric,
    market_cap numeric,
    fiscal_date date,
    known_from date
)
LANGUAGE sql STABLE PARALLEL SAFE
AS $fn$
WITH
span AS (
    SELECT coalesce(fn_daily_market_cap.start_date, '-infinity'::date) AS lo,
           coalesce(fn_daily_market_cap.end_date, 'infinity'::date) AS hi
),
bars AS MATERIALIZED (
    SELECT p.security_id AS sid, p.trade_date AS d, p.close
      FROM mart.v_daily_price_raw p
     WHERE p.security_id = ANY (fn_daily_market_cap.security_ids)
       AND p.trade_date >= coalesce(fn_daily_market_cap.start_date, '-infinity'::date)
       AND p.trade_date <= coalesce(fn_daily_market_cap.end_date, 'infinity'::date)
),
bs AS MATERIALIZED (
    -- Every balance sheet with a count that can be in force somewhere in the span.
    SELECT b.security_id AS sid, b.known_from AS kf, b.known_to AS kt,
           b.fiscal_date AS fd
      FROM mart.v_balance_sheet_versions b, span
     WHERE b.security_id = ANY (fn_daily_market_cap.security_ids)
       AND b.superseded_at IS NULL AND b.shares_outstanding > 0
       AND b.known_from <= span.hi
       AND (b.known_to IS NULL OR b.known_to > span.lo)
       AND b.fiscal_date >= span.lo - 730
),
sf AS MATERIALIZED (
    -- The income versions whose factor can apply in the span.
    SELECT f.security_id AS sid, f.known_from AS kf, f.known_to AS kt
      FROM mart.v_share_factor_versions f, span
     WHERE f.security_id = ANY (fn_daily_market_cap.security_ids)
       AND f.superseded_at IS NULL
       AND f.known_from <= span.hi
       AND (f.known_to IS NULL OR f.known_to > span.lo)
       AND f.fiscal_date >= span.lo - 730
),
splits AS MATERIALIZED (
    -- The warehouse's splits, the list the raw prices follow (ADR 0019 §3).
    SELECT a.security_id AS sid,
           array_agg(a.ex_date ORDER BY a.ex_date) AS ex,
           array_agg(ln(a.split_numerator / a.split_denominator) ORDER BY a.ex_date) AS l
      FROM mart.v_corporate_action a
     WHERE a.action_type = 'split' AND a.split_numerator > 0 AND a.split_denominator > 0
       AND a.split_kind = 'shares'  -- 0048: a spin factor changes no count
       AND a.security_id = ANY (fn_daily_market_cap.security_ids)
     GROUP BY a.security_id
),
cuts AS MATERIALIZED (
    -- The days anything in force can change, clamped to the span.
    SELECT DISTINCT c.sid, greatest(c.d, span.lo) AS d
      FROM (
            SELECT sid, kf AS d FROM bs
            UNION ALL SELECT sid, kt FROM bs WHERE kt IS NOT NULL
            UNION ALL SELECT sid, fd + 731 FROM bs     -- the first day it is stale
            UNION ALL SELECT sid, kf FROM sf
            UNION ALL SELECT sid, kt FROM sf WHERE kt IS NOT NULL
            UNION ALL SELECT s.sid, u.e FROM splits s, unnest(s.ex) AS u(e)
           ) c, span
     WHERE c.d <= span.hi
),
seg AS MATERIALIZED (
    -- Per segment: the count in force on its first day, the same period's factor,
    -- and the splits since the count was known.
    SELECT c.sid, c.d, row_number() OVER (PARTITION BY c.sid ORDER BY c.d) AS n,
           b.fiscal_date, b.known_from,
           round(b.shares_outstanding
                 * (CASE WHEN sp.ex IS NULL THEN 1 ELSE (SELECT exp(coalesce(sum(u.l), 0))
                      FROM unnest(sp.ex, sp.l) AS u(e, l)
                     WHERE u.e > b.known_from AND u.e <= c.d) END)) AS so_as_of,
           coalesce(f.share_factor, 1) AS share_factor
      FROM cuts c
      LEFT JOIN LATERAL (
            SELECT v.period_type, v.fiscal_date, v.known_from, v.shares_outstanding
              FROM mart.v_balance_sheet_versions v
             WHERE v.security_id = c.sid
               AND v.superseded_at IS NULL AND v.shares_outstanding > 0
               AND v.known_from <= c.d AND (v.known_to IS NULL OR c.d < v.known_to)
               AND v.fiscal_date >= c.d - 730
             ORDER BY v.fiscal_date DESC, v.known_from DESC, v.period_type
             LIMIT 1) b ON true
      LEFT JOIN LATERAL (
            SELECT w.share_factor
              FROM mart.v_share_factor_versions w
             WHERE w.security_id = c.sid AND w.period_type = b.period_type
               AND w.fiscal_date = b.fiscal_date
               AND w.superseded_at IS NULL
               AND w.known_from <= c.d AND (w.known_to IS NULL OR c.d < w.known_to)
             ORDER BY w.known_from DESC
             LIMIT 1) f ON true
      LEFT JOIN splits sp ON sp.sid = c.sid
),
placed AS MATERIALIZED (
    -- Each bar's segment: how many segments have begun by its day (a segment
    -- beginning on the bar's own day counts).
    SELECT x.sid, x.d, x.close, x.n
      FROM (
            SELECT u.sid, u.d, u.close, u.k,
                   count(*) FILTER (WHERE u.k = 0) OVER (
                       PARTITION BY u.sid ORDER BY u.d, u.k
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS n
              FROM (SELECT sid, d, close, 1 AS k FROM bars
                    UNION ALL SELECT sid, d, NULL, 0 FROM cuts) u
           ) x
     WHERE x.k = 1
)
SELECT p.sid,
       p.d,
       coalesce(sp.currency, 'USD'),
       p.close,
       s.so_as_of,
       CASE WHEN s.so_as_of IS NOT NULL THEN s.share_factor END,
       p.close * s.so_as_of * s.share_factor,
       s.fiscal_date,
       s.known_from
  FROM placed p
  LEFT JOIN seg s ON s.sid = p.sid AND s.n = p.n
  LEFT JOIN mart.v_security_profile sp ON sp.security_id = p.sid
 ORDER BY p.sid, p.d
$fn$;

COMMENT ON FUNCTION mart.fn_daily_market_cap(bigint[], date, date) IS
    'Daily market cap per raw bar (0042; ADR 0019 §5): the raw close x the shares of '
    'the latest balance sheet in force that day (ended within 730 days), in that '
    'day''s share terms with the warehouse''s splits, x SF1''s share factor for the '
    'period (mart.v_share_factor_versions; 1 where SF1 gave none). In the trading '
    'currency. NULL where no count is in force: funds, ETFs, secondary share classes, '
    'unfed securities, stale statements. Derived on read; NULL dates leave the span '
    'open.'
    ' The splits a count follows are share splits: a spin factor is none '
    '(0048).';

-- ---------------------------------------------------------------------------
-- mart.v_security_attributes: 0042's, the same filter
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_security_attributes AS
SELECT
    s.security_id,
    s.primary_symbol AS symbol,
    s.company_name,
    s.asset_type,
    s.exchange_code,
    sec.sector_name,
    ind.industry_name,
    c.attributes_source,
    s.currency,
    s.country,
    s.is_actively_trading,
    s.is_etf,
    s.is_fund,
    CASE WHEN mc.market_cap IS NOT NULL THEN mc.market_cap ELSE s.market_cap_usd END
        AS market_cap_usd,
    CASE WHEN mc.market_cap IS NOT NULL THEN 'derived'
         WHEN s.market_cap_usd IS NOT NULL THEN s.source END
        AS market_cap_source,
    CASE WHEN mc.market_cap IS NOT NULL THEN lp.trade_date END AS market_cap_as_of,
    mc.fiscal_date AS market_cap_fiscal_date,
    CASE WHEN bt.beta IS NOT NULL THEN bt.beta ELSE s.beta END AS beta,
    CASE WHEN bt.beta IS NOT NULL THEN 'derived'
         WHEN s.beta IS NOT NULL THEN s.source END
        AS beta_source,
    bt.as_of AS beta_as_of,
    bt.returns AS beta_returns,
    lp.trade_date AS last_trade_date,
    lp.close AS last_close,
    lp.volume AS last_volume
FROM core.security s
JOIN core.v_security_classification c ON c.security_id = s.security_id
LEFT JOIN ref.sector sec ON sec.sector_id = c.sector_id
LEFT JOIN ref.industry ind ON ind.industry_id = c.industry_id
LEFT JOIN LATERAL (
    SELECT dp.trade_date, dp.close, dp.volume
      FROM core.daily_price dp
     WHERE dp.security_id = s.security_id
     ORDER BY dp.trade_date DESC
     LIMIT 1
) lp ON true
LEFT JOIN LATERAL (
    -- ADR 0019 §5, mart.fn_daily_market_cap's expression on the last bar: the raw
    -- close x the shares of the latest balance sheet in force that day, in that day's
    -- terms, x the same period's share factor in force that day (1 where none).
    SELECT b.fiscal_date,
           lp.close * round(b.shares_outstanding * (
               SELECT exp(coalesce(sum(ln(a.split_numerator::numeric
                                          / a.split_denominator)), 0))
                 FROM mart.v_corporate_action a
                WHERE a.security_id = s.security_id AND a.action_type = 'split'
                  AND a.split_numerator > 0 AND a.split_denominator > 0
                  AND a.split_kind = 'shares'  -- 0048: a spin factor changes no count
                  AND a.ex_date > b.known_from AND a.ex_date <= lp.trade_date))
               * coalesce((
                   SELECT f.share_factor
                     FROM mart.v_share_factor_versions f
                    WHERE f.security_id = s.security_id
                      AND f.period_type = b.period_type
                      AND f.fiscal_date = b.fiscal_date
                      AND f.superseded_at IS NULL
                      AND f.known_from <= lp.trade_date
                      AND (f.known_to IS NULL OR lp.trade_date < f.known_to)
                    ORDER BY f.known_from DESC
                    LIMIT 1), 1)
               AS market_cap
      FROM core.balance_sheet b
     WHERE b.security_id = s.security_id
       AND coalesce(s.currency, 'USD') = 'USD'
       AND b.superseded_at IS NULL AND b.shares_outstanding > 0
       AND b.known_from <= lp.trade_date
       AND (b.known_to IS NULL OR lp.trade_date < b.known_to)
       AND b.fiscal_date >= lp.trade_date - 730
     ORDER BY b.fiscal_date DESC, b.known_from DESC, b.period_type
     LIMIT 1
) mc ON true
LEFT JOIN (
    -- A slope too steep for the screener's numeric(12,6) is a degenerate series,
    -- not a beta.
    SELECT f.security_id, f.beta, f.returns, f.as_of_date AS as_of
      FROM mart.fn_security_betas(current_date) f
     WHERE abs(f.beta) < 1000000
) bt ON bt.security_id = s.security_id;

COMMENT ON VIEW mart.v_security_attributes IS
    'Per security, the screener''s attributes by precedence (ADR 0015 §4, #109), with '
    'their sources and dates. Classification from core.v_security_classification. '
    'market_cap_usd derived (ADR 0019 §5: the last raw close x SF1 shares in force '
    'that day, in its terms, x the period''s share factor (0042); USD-traded) where '
    'SF1 holds the shares, else core.security''s vendor snapshot. beta derived (365 '
    'days of daily adjusted returns on SPY''s, at least 120) else core.security''s. '
    'Computes the whole universe''s betas when read: read mart.security_latest, '
    'refreshed nightly.'
    ' A spin factor is not a share split (0048).';

-- ---------------------------------------------------------------------------
-- mart.fn_fundamental_panel: 0043's, the same filter
-- ---------------------------------------------------------------------------
-- Same arguments and return type, so mart.fundamental_panel and its readers stand;
-- they take the change on their next refresh.
CREATE OR REPLACE FUNCTION mart.fn_fundamental_panel(
    as_of_dates date[], extra_lag_days integer DEFAULT 0
)
RETURNS TABLE (
    security_id bigint,
    as_of_date date,
    exchange_code text,
    sector_name text,
    industry_name text,
    trading_currency text,
    reported_currency text,
    currency_mismatch boolean,
    basis text,
    fiscal_date date,
    fiscal_year integer,
    fiscal_period text,
    known_from date,
    knowledge_source text,
    days_since_filing integer,
    ttm_quarters_used integer,
    price_date date,
    close_raw numeric,
    close_adjusted numeric,
    shares_outstanding_as_of numeric,
    market_cap numeric,
    enterprise_value numeric,
    earnings_yield double precision,
    book_to_price double precision,
    sales_to_price double precision,
    cash_flow_to_price double precision,
    fcf_yield double precision,
    ev_to_ebitda double precision,
    ev_to_sales double precision,
    ev_to_ebit double precision,
    ev_to_fcf double precision,
    roe double precision,
    roa double precision,
    roic double precision,
    gross_profitability double precision,
    gross_margin double precision,
    operating_margin double precision,
    net_margin double precision,
    asset_turnover double precision,
    accruals double precision,
    net_operating_assets double precision,
    debt_to_equity double precision,
    net_debt_to_ebitda double precision,
    interest_coverage double precision,
    current_ratio double precision,
    quick_ratio double precision,
    altman_z double precision,
    piotroski_f integer,
    revenue_growth_yoy double precision,
    eps_growth_yoy double precision,
    revenue_cagr_3y double precision,
    net_income_growth_yoy double precision,
    asset_growth_1y double precision,
    capex_to_sales double precision,
    net_share_issuance_1y double precision,
    dividend_yield_ttm double precision,
    buyback_yield_ttm double precision,
    shareholder_yield double precision,
    share_factor numeric  -- 0043
)
LANGUAGE sql STABLE PARALLEL SAFE
-- The materialized steps hide their row counts from the planner, which would loop
-- over one inside another: quadratic. Every set-based step here wants a hash or a
-- merge join; the per-row lookups (dividends, splits) are LATERAL or correlated, and
-- a nested loop still runs those. The penalty that steers the planner off nested
-- loops also lifts the plan's cost over the JIT thresholds, and compiling it would
-- take longer than running it.
SET enable_nestloop = off
SET jit = off
AS $fn$
WITH
dates AS MATERIALIZED (
    -- The cross-sections asked for: statements as known at known_at, prices at
    -- as_of_date. A negative lag would read ahead, so it gives nothing.
    SELECT DISTINCT u.d AS as_of_date, u.d - extra_lag_days AS known_at
      FROM unnest(as_of_dates) AS u(d)
     WHERE u.d IS NOT NULL AND extra_lag_days >= 0
),
span AS MATERIALIZED (
    -- frame_lo: the oldest period any of them can read. A basis ended within
    -- 730 days of known_at, and its three-year-ago period within 1826
    -- days before that.
    SELECT min(known_at) - 2556 AS frame_lo,
           max(known_at) AS hi,
           min(as_of_date) AS first_date,
           max(as_of_date) AS last_date,
           array_agg(known_at ORDER BY known_at) AS knowns
      FROM dates
),
iq AS MATERIALIZED (
    SELECT v.security_id AS sid,
           v.fiscal_date AS fd,
           v.fiscal_year * 4 + substr(v.fiscal_period, 2)::int AS qn,
           v.fiscal_year,
           v.fiscal_period,
           v.reported_currency,
           v.known_from AS kf,
           coalesce(v.known_to, 'infinity') AS kt,
           v.knowledge_source AS src,
           v.revenue,
           v.cost_of_revenue,
           v.gross_profit,
           v.operating_income,
           v.ebit,
           v.income_tax_expense,
           v.interest_expense,
           v.net_income,
           v.net_income_common,
           v.weighted_average_shares,
           sf.share_factor  -- 0043
      FROM mart.v_income_statement_versions v
      CROSS JOIN span
      -- 0043: the same row's factor (mart.v_share_factor_versions, 0042).
      LEFT JOIN mart.v_share_factor_versions sf
        ON sf.security_id = v.security_id AND sf.period_type = v.period_type
       AND sf.fiscal_date = v.fiscal_date AND sf.version_no = v.version_no
       AND sf.loaded_at = v.loaded_at
     WHERE v.superseded_at IS NULL AND v.period_type = 'quarter'
       AND v.fiscal_date >= span.frame_lo - 378 AND v.known_from <= span.hi
),
cq AS MATERIALIZED (
    SELECT v.security_id AS sid,
           v.fiscal_date AS fd,
           v.fiscal_year * 4 + substr(v.fiscal_period, 2)::int AS qn,
           v.known_from AS kf,
           coalesce(v.known_to, 'infinity') AS kt,
           v.knowledge_source AS src,
           v.net_cash_operating,
           v.depreciation_amortization,
           v.capital_expenditure,
           v.dividends_paid,
           v.net_equity_issuance
      FROM mart.v_cash_flow_statement_versions v, span
     WHERE v.superseded_at IS NULL AND v.period_type = 'quarter'
       AND v.fiscal_date >= span.frame_lo - 378 AND v.known_from <= span.hi
),
bq AS MATERIALIZED (
    SELECT v.security_id AS sid,
           v.fiscal_date AS fd,
           v.fiscal_year * 4 + substr(v.fiscal_period, 2)::int AS qn,
           v.known_from AS kf,
           coalesce(v.known_to, 'infinity') AS kt,
           v.knowledge_source AS src,
           v.total_assets,
           v.total_current_assets,
           v.total_current_liabilities,
           v.inventory,
           v.cash_and_equivalents,
           v.total_debt,
           v.debt_noncurrent,
           v.total_liabilities,
           v.total_equity_parent,
           v.retained_earnings,
           v.shares_outstanding
      FROM mart.v_balance_sheet_versions v, span
     WHERE v.superseded_at IS NULL AND v.period_type = 'quarter'
       AND v.fiscal_date >= span.frame_lo AND v.known_from <= span.hi
),
ia AS MATERIALIZED (
    SELECT v.security_id AS sid,
           v.fiscal_date AS fd,
           v.fiscal_year,
           v.fiscal_period,
           v.reported_currency,
           v.known_from AS kf,
           coalesce(v.known_to, 'infinity') AS kt,
           v.knowledge_source AS src,
           v.revenue,
           v.cost_of_revenue,
           v.gross_profit,
           v.operating_income,
           v.ebit,
           v.income_tax_expense,
           v.interest_expense,
           v.net_income,
           v.net_income_common,
           v.weighted_average_shares,
           sf.share_factor  -- 0043
      FROM mart.v_income_statement_versions v
      CROSS JOIN span
      -- 0043: the same row's factor (mart.v_share_factor_versions, 0042).
      LEFT JOIN mart.v_share_factor_versions sf
        ON sf.security_id = v.security_id AND sf.period_type = v.period_type
       AND sf.fiscal_date = v.fiscal_date AND sf.version_no = v.version_no
       AND sf.loaded_at = v.loaded_at
     WHERE v.superseded_at IS NULL AND v.period_type = 'annual'
       AND v.fiscal_date >= span.frame_lo AND v.known_from <= span.hi
),
ca AS MATERIALIZED (
    SELECT v.security_id AS sid,
           v.fiscal_date AS fd,
           v.known_from AS kf,
           coalesce(v.known_to, 'infinity') AS kt,
           v.knowledge_source AS src,
           v.net_cash_operating,
           v.depreciation_amortization,
           v.capital_expenditure,
           v.dividends_paid,
           v.net_equity_issuance
      FROM mart.v_cash_flow_statement_versions v, span
     WHERE v.superseded_at IS NULL AND v.period_type = 'annual'
       AND v.fiscal_date >= span.frame_lo AND v.known_from <= span.hi
),
ba AS MATERIALIZED (
    SELECT v.security_id AS sid,
           v.fiscal_date AS fd,
           v.known_from AS kf,
           coalesce(v.known_to, 'infinity') AS kt,
           v.knowledge_source AS src,
           v.total_assets,
           v.total_current_assets,
           v.total_current_liabilities,
           v.inventory,
           v.cash_and_equivalents,
           v.total_debt,
           v.debt_noncurrent,
           v.total_liabilities,
           v.total_equity_parent,
           v.retained_earnings,
           v.shares_outstanding
      FROM mart.v_balance_sheet_versions v, span
     WHERE v.superseded_at IS NULL AND v.period_type = 'annual'
       AND v.fiscal_date >= span.frame_lo AND v.known_from <= span.hi
),
px AS MATERIALIZED (
    -- Only while it traded: each security's last close in the week to each date. One
    -- pass over the bars of those weeks (the bounds prune the partitions).
    SELECT DISTINCT ON (r.security_id, k.as_of_date)
           r.security_id AS sid, k.as_of_date, k.known_at,
           r.trade_date AS price_date, r.close AS close_raw
      FROM (SELECT d.as_of_date, d.known_at, d.as_of_date - g.n AS day
              FROM dates d CROSS JOIN generate_series(0, 6) AS g(n)) k
      JOIN mart.v_daily_price_raw r ON r.trade_date = k.day
     WHERE r.close > 0
       AND r.trade_date > (SELECT first_date - 7 FROM span)
       AND r.trade_date <= (SELECT last_date FROM span)
       AND r.security_id IN (SELECT sid FROM iq UNION SELECT sid FROM ia)
     ORDER BY r.security_id, k.as_of_date, r.trade_date DESC
),
secs AS MATERIALIZED (
    SELECT DISTINCT sid FROM px
),
ipos AS MATERIALIZED (
    -- Each quarter-end's place in its security's run, and how many end in the
    -- 378 days to it: the most a TTM can reach back.
    SELECT p.sid, p.fd, row_number() OVER w AS pos,
           count(*) OVER (w RANGE BETWEEN interval '378 days' PRECEDING
                          AND CURRENT ROW) AS n
      FROM (SELECT DISTINCT q.sid, q.fd FROM iq q JOIN secs l ON l.sid = q.sid) p
    WINDOW w AS (PARTITION BY p.sid ORDER BY p.fd)
),
cpos AS MATERIALIZED (
    -- Each quarter-end's place in its security's run, and how many end in the
    -- 378 days to it: the most a TTM can reach back.
    SELECT p.sid, p.fd, row_number() OVER w AS pos,
           count(*) OVER (w RANGE BETWEEN interval '378 days' PRECEDING
                          AND CURRENT ROW) AS n
      FROM (SELECT DISTINCT q.sid, q.fd FROM cq q JOIN secs l ON l.sid = q.sid) p
    WINDOW w AS (PARTITION BY p.sid ORDER BY p.fd)
),
imem AS MATERIALIZED (
    -- The quarters a TTM at qfd can read: those ending in the 378 days to it.
    SELECT q.sid, q.qfd, m.fd
      FROM (SELECT p.sid, p.fd AS qfd, generate_series(p.pos - p.n + 1, p.pos) AS pos
              FROM ipos p, span
             WHERE p.fd >= span.frame_lo) q
      JOIN ipos m ON m.sid = q.sid AND m.pos = q.pos
),
cmem AS MATERIALIZED (
    -- The quarters a TTM at qfd can read: those ending in the 378 days to it.
    SELECT q.sid, q.qfd, m.fd
      FROM (SELECT p.sid, p.fd AS qfd, generate_series(p.pos - p.n + 1, p.pos) AS pos
              FROM cpos p, span
             WHERE p.fd >= span.frame_lo) q
      JOIN cpos m ON m.sid = q.sid AND m.pos = q.pos
),
qbp AS MATERIALIZED (
    SELECT DISTINCT x.sid, x.qfd, x.b
      FROM (SELECT m.sid, m.qfd, unnest(ARRAY[v.kf, v.kt]) AS b
              FROM imem m JOIN iq v ON v.sid = m.sid AND v.fd = m.fd
            UNION ALL
            SELECT m.sid, m.qfd, unnest(ARRAY[v.kf, v.kt]) AS b
              FROM cmem m JOIN cq v ON v.sid = m.sid AND v.fd = m.fd
            UNION ALL
            SELECT v.sid, v.fd, unnest(ARRAY[v.kf, v.kt]) AS b
              FROM bq v JOIN secs l ON l.sid = v.sid) x
      JOIN (SELECT sid, fd, min(kf) AS kf FROM iq GROUP BY sid, fd) f
        ON f.sid = x.sid AND f.fd = x.qfd
      CROSS JOIN span
     WHERE x.b >= f.kf AND x.b <= span.hi
),
qst AS MATERIALIZED (
    -- Between two edges nothing changes. A state no requested date falls in is
    -- never read, and is not computed.
    SELECT s.sid, s.qfd, s.b, s.e
      FROM (SELECT p.sid, p.qfd, p.b,
                   coalesce(lead(p.b) OVER (PARTITION BY p.sid, p.qfd ORDER BY p.b),
                            'infinity') AS e
              FROM qbp p) s, span
     WHERE width_bucket(s.e - 1, span.knowns) > width_bucket(s.b - 1, span.knowns)
),
iw AS MATERIALIZED (
    SELECT w.*,
           coalesce((w.qn - w.qn1 = 1 AND w.fd - w.d1 BETWEEN 56 AND 126), false) AS c1,
           coalesce((w.qn - w.qn1 = 1 AND w.fd - w.d1 BETWEEN 56 AND 126) AND (w.qn1 - w.qn2 = 1 AND w.d1 - w.d2 BETWEEN 56 AND 126), false) AS c2,
           coalesce((w.qn - w.qn1 = 1 AND w.fd - w.d1 BETWEEN 56 AND 126) AND (w.qn1 - w.qn2 = 1 AND w.d1 - w.d2 BETWEEN 56 AND 126) AND (w.qn2 - w.qn3 = 1 AND w.d2 - w.d3 BETWEEN 56 AND 126), false) AS c3
      FROM (SELECT st.sid,
                   st.qfd,
                   st.b,
                   st.e,
                   v.fd,
                   v.qn,
                   v.kf,
                   v.src,
                   v.fiscal_year,
                   v.fiscal_period,
                   v.reported_currency,
                   v.weighted_average_shares,
                   v.share_factor,  -- 0043
                   lag(v.qn, 1) OVER w AS qn1,
                   lag(v.fd, 1) OVER w AS d1,
                   lag(v.kf, 1) OVER w AS k1,
                   lag(v.src, 1) OVER w AS s1,
                   lag(v.qn, 2) OVER w AS qn2,
                   lag(v.fd, 2) OVER w AS d2,
                   lag(v.kf, 2) OVER w AS k2,
                   lag(v.src, 2) OVER w AS s2,
                   lag(v.qn, 3) OVER w AS qn3,
                   lag(v.fd, 3) OVER w AS d3,
                   lag(v.kf, 3) OVER w AS k3,
                   lag(v.src, 3) OVER w AS s3,
                   sum(v.revenue) OVER f AS revenue_sum,
                   count(v.revenue) OVER f AS revenue_n,
                   sum(v.cost_of_revenue) OVER f AS cost_of_revenue_sum,
                   count(v.cost_of_revenue) OVER f AS cost_of_revenue_n,
                   sum(v.gross_profit) OVER f AS gross_profit_sum,
                   count(v.gross_profit) OVER f AS gross_profit_n,
                   sum(v.operating_income) OVER f AS operating_income_sum,
                   count(v.operating_income) OVER f AS operating_income_n,
                   sum(v.ebit) OVER f AS ebit_sum,
                   count(v.ebit) OVER f AS ebit_n,
                   sum(v.income_tax_expense) OVER f AS income_tax_expense_sum,
                   count(v.income_tax_expense) OVER f AS income_tax_expense_n,
                   sum(v.interest_expense) OVER f AS interest_expense_sum,
                   count(v.interest_expense) OVER f AS interest_expense_n,
                   sum(v.net_income) OVER f AS net_income_sum,
                   count(v.net_income) OVER f AS net_income_n,
                   sum(v.net_income_common) OVER f AS net_income_common_sum,
                   count(v.net_income_common) OVER f AS net_income_common_n
              FROM qst st
              JOIN imem m ON m.sid = st.sid AND m.qfd = st.qfd
              JOIN iq v ON v.sid = m.sid AND v.fd = m.fd
                       AND v.kf <= st.b AND st.b < v.kt
            WINDOW w AS (PARTITION BY st.sid, st.qfd, st.b ORDER BY v.fd),
                   f AS (w ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)) w
     WHERE w.fd = w.qfd
),
cw AS MATERIALIZED (
    SELECT w.*,
           coalesce((w.qn - w.qn1 = 1 AND w.fd - w.d1 BETWEEN 56 AND 126), false) AS c1,
           coalesce((w.qn - w.qn1 = 1 AND w.fd - w.d1 BETWEEN 56 AND 126) AND (w.qn1 - w.qn2 = 1 AND w.d1 - w.d2 BETWEEN 56 AND 126), false) AS c2,
           coalesce((w.qn - w.qn1 = 1 AND w.fd - w.d1 BETWEEN 56 AND 126) AND (w.qn1 - w.qn2 = 1 AND w.d1 - w.d2 BETWEEN 56 AND 126) AND (w.qn2 - w.qn3 = 1 AND w.d2 - w.d3 BETWEEN 56 AND 126), false) AS c3
      FROM (SELECT st.sid,
                   st.qfd,
                   st.b,
                   st.e,
                   v.fd,
                   v.qn,
                   v.kf,
                   v.src,
                   lag(v.qn, 1) OVER w AS qn1,
                   lag(v.fd, 1) OVER w AS d1,
                   lag(v.kf, 1) OVER w AS k1,
                   lag(v.src, 1) OVER w AS s1,
                   lag(v.qn, 2) OVER w AS qn2,
                   lag(v.fd, 2) OVER w AS d2,
                   lag(v.kf, 2) OVER w AS k2,
                   lag(v.src, 2) OVER w AS s2,
                   lag(v.qn, 3) OVER w AS qn3,
                   lag(v.fd, 3) OVER w AS d3,
                   lag(v.kf, 3) OVER w AS k3,
                   lag(v.src, 3) OVER w AS s3,
                   sum(v.net_cash_operating) OVER f AS net_cash_operating_sum,
                   count(v.net_cash_operating) OVER f AS net_cash_operating_n,
                   sum(v.depreciation_amortization) OVER f AS depreciation_amortization_sum,
                   count(v.depreciation_amortization) OVER f AS depreciation_amortization_n,
                   sum(v.capital_expenditure) OVER f AS capital_expenditure_sum,
                   count(v.capital_expenditure) OVER f AS capital_expenditure_n,
                   sum(v.dividends_paid) OVER f AS dividends_paid_sum,
                   count(v.dividends_paid) OVER f AS dividends_paid_n,
                   sum(v.net_equity_issuance) OVER f AS net_equity_issuance_sum,
                   count(v.net_equity_issuance) OVER f AS net_equity_issuance_n
              FROM qst st
              JOIN cmem m ON m.sid = st.sid AND m.qfd = st.qfd
              JOIN cq v ON v.sid = m.sid AND v.fd = m.fd
                       AND v.kf <= st.b AND st.b < v.kt
            WINDOW w AS (PARTITION BY st.sid, st.qfd, st.b ORDER BY v.fd),
                   f AS (w ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)) w
     WHERE w.fd = w.qfd
),
abp AS MATERIALIZED (
    SELECT DISTINCT x.sid, x.fd, x.b
      FROM (SELECT v.sid, v.fd, unnest(ARRAY[v.kf, v.kt]) AS b
              FROM ia v JOIN secs l ON l.sid = v.sid
            UNION ALL
            SELECT v.sid, v.fd, unnest(ARRAY[v.kf, v.kt]) AS b
              FROM ba v JOIN secs l ON l.sid = v.sid
            UNION ALL
            SELECT v.sid, v.fd, unnest(ARRAY[v.kf, v.kt]) AS b
              FROM ca v JOIN secs l ON l.sid = v.sid) x
      JOIN (SELECT sid, fd, min(kf) AS kf FROM ia GROUP BY sid, fd) f
        ON f.sid = x.sid AND f.fd = x.fd
      CROSS JOIN span
     WHERE x.b >= f.kf AND x.b <= span.hi
),
ast AS MATERIALIZED (
    -- Between two edges nothing changes. A state no requested date falls in is
    -- never read, and is not computed.
    SELECT s.sid, s.fd, s.b, s.e
      FROM (SELECT p.sid, p.fd, p.b,
                   coalesce(lead(p.b) OVER (PARTITION BY p.sid, p.fd ORDER BY p.b),
                            'infinity') AS e
              FROM abp p) s, span
     WHERE width_bucket(s.e - 1, span.knowns) > width_bucket(s.b - 1, span.knowns)
),
frame AS MATERIALIZED (
    -- Every candidate basis at every state: a TTM of four contiguous quarters
    -- (mart.fn_fundamentals_ttm's), and a fiscal year with its income statement.
    -- A row is in force on [b, e).
    SELECT i.sid,
           'ttm'::text AS basis,
           i.qfd AS fd,
           i.qn AS seq,
           i.b,
           i.e,
           i.c3 AS cand,
           i.fiscal_year,
           i.fiscal_period,
           i.reported_currency,
           greatest(i.kf, CASE WHEN i.c1 THEN i.k1 END, CASE WHEN i.c2 THEN i.k2 END,
                    CASE WHEN i.c3 THEN i.k3 END, c.kf,
                    CASE WHEN c.c1 THEN c.k1 END, CASE WHEN c.c2 THEN c.k2 END,
                    CASE WHEN c.c3 THEN c.k3 END, bal.kf) AS known_from,
           CASE WHEN 'estimated' = ANY(ARRAY[i.src, CASE WHEN i.c1 THEN i.s1 END, CASE WHEN i.c2 THEN i.s2 END, CASE WHEN i.c3 THEN i.s3 END, c.src, CASE WHEN c.c1 THEN c.s1 END, CASE WHEN c.c2 THEN c.s2 END, CASE WHEN c.c3 THEN c.s3 END, bal.src]) THEN 'estimated'
                WHEN 'observed' = ANY(ARRAY[i.src, CASE WHEN i.c1 THEN i.s1 END, CASE WHEN i.c2 THEN i.s2 END, CASE WHEN i.c3 THEN i.s3 END, c.src, CASE WHEN c.c1 THEN c.s1 END, CASE WHEN c.c2 THEN c.s2 END, CASE WHEN c.c3 THEN c.s3 END, bal.src]) THEN 'observed'
                ELSE 'filed' END AS knowledge_source,
           i.kf AS income_known_from,
           bal.kf AS balance_sheet_known_from,
           1 + i.c1::int + i.c2::int + i.c3::int AS ttm_quarters_used,
           CASE WHEN i.c3 AND i.revenue_n = 4 THEN i.revenue_sum END AS revenue,
           CASE WHEN i.c3 AND i.cost_of_revenue_n = 4 THEN i.cost_of_revenue_sum END AS cost_of_revenue,
           CASE WHEN i.c3 AND i.gross_profit_n = 4 THEN i.gross_profit_sum END AS gross_profit,
           CASE WHEN i.c3 AND i.operating_income_n = 4 THEN i.operating_income_sum END AS operating_income,
           CASE WHEN i.c3 AND i.ebit_n = 4 THEN i.ebit_sum END AS ebit,
           CASE WHEN i.c3 AND i.income_tax_expense_n = 4 THEN i.income_tax_expense_sum END AS income_tax_expense,
           CASE WHEN i.c3 AND i.interest_expense_n = 4 THEN i.interest_expense_sum END AS interest_expense,
           CASE WHEN i.c3 AND i.net_income_n = 4 THEN i.net_income_sum END AS net_income,
           CASE WHEN i.c3 AND i.net_income_common_n = 4 THEN i.net_income_common_sum END AS net_income_common,
           i.weighted_average_shares,
           CASE WHEN c.c3 AND c.net_cash_operating_n = 4 THEN c.net_cash_operating_sum END AS net_cash_operating,
           CASE WHEN c.c3 AND c.depreciation_amortization_n = 4 THEN c.depreciation_amortization_sum END AS depreciation_amortization,
           CASE WHEN c.c3 AND c.capital_expenditure_n = 4 THEN c.capital_expenditure_sum END AS capital_expenditure,
           CASE WHEN c.c3 AND c.dividends_paid_n = 4 THEN c.dividends_paid_sum END AS dividends_paid,
           CASE WHEN c.c3 AND c.net_equity_issuance_n = 4 THEN c.net_equity_issuance_sum END AS net_equity_issuance,
           bal.total_assets,
           bal.total_current_assets,
           bal.total_current_liabilities,
           bal.inventory,
           bal.cash_and_equivalents,
           bal.total_debt,
           bal.debt_noncurrent,
           bal.total_liabilities,
           bal.total_equity_parent,
           bal.retained_earnings,
           bal.shares_outstanding,
           i.share_factor  -- 0043: the basis quarter's version's
      FROM iw i
      LEFT JOIN cw c ON c.sid = i.sid AND c.qfd = i.qfd AND c.b = i.b
      LEFT JOIN bq bal ON bal.sid = i.sid AND bal.fd = i.qfd
                      AND bal.kf <= i.b AND i.b < bal.kt
    UNION ALL
    SELECT st.sid,
           'annual',
           st.fd,
           a.fiscal_year * 4 + 4,
           st.b,
           st.e,
           true,
           a.fiscal_year,
           a.fiscal_period,
           a.reported_currency,
           greatest(a.kf, bal.kf, c.kf),
           CASE WHEN 'estimated' = ANY(ARRAY[a.src, bal.src, c.src]) THEN 'estimated'
                WHEN 'observed' = ANY(ARRAY[a.src, bal.src, c.src]) THEN 'observed'
                ELSE 'filed' END,
           a.kf,
           bal.kf,
           NULL::integer,
           a.revenue,
           a.cost_of_revenue,
           a.gross_profit,
           a.operating_income,
           a.ebit,
           a.income_tax_expense,
           a.interest_expense,
           a.net_income,
           a.net_income_common,
           a.weighted_average_shares,
           c.net_cash_operating,
           c.depreciation_amortization,
           c.capital_expenditure,
           c.dividends_paid,
           c.net_equity_issuance,
           bal.total_assets,
           bal.total_current_assets,
           bal.total_current_liabilities,
           bal.inventory,
           bal.cash_and_equivalents,
           bal.total_debt,
           bal.debt_noncurrent,
           bal.total_liabilities,
           bal.total_equity_parent,
           bal.retained_earnings,
           bal.shares_outstanding,
           a.share_factor  -- 0043: the year's version's
      FROM ast st
      JOIN ia a ON a.sid = st.sid AND a.fd = st.fd AND a.kf <= st.b AND st.b < a.kt
      LEFT JOIN ba bal ON bal.sid = st.sid AND bal.fd = st.fd
                      AND bal.kf <= st.b AND st.b < bal.kt
      LEFT JOIN ca c ON c.sid = st.sid AND c.fd = st.fd AND c.kf <= st.b AND st.b < c.kt
),
runs AS MATERIALIZED (
    -- Each candidate's runs of consecutive states: when it could be the basis. The
    -- key orders candidates by fiscal_date, a TTM ahead of a year on a tie.
    SELECT g.sid, g.key, min(g.b) AS start, max(g.e) AS stop
      FROM (SELECT f.sid, f.b, f.e, f.cand,
                   (f.fd - DATE '1900-01-01') * 2 + (f.basis = 'ttm')::int AS key,
                   count(*) FILTER (WHERE NOT f.cand)
                       OVER (PARTITION BY f.sid, f.basis, f.fd ORDER BY f.b) AS grp
              FROM frame f) g
     WHERE g.cand
     GROUP BY g.sid, g.key, g.grp
),
swept AS MATERIALIZED (
    -- The latest candidate whose run began by known_at and never ends.
    SELECT s.sid, s.as_of_date, s.key
      FROM (SELECT e.sid, e.as_of_date, e.kind,
                   max(e.key) OVER (PARTITION BY e.sid ORDER BY e.t, e.kind
                                    ROWS UNBOUNDED PRECEDING) AS key
              FROM (SELECT r.sid, r.start AS t, 0 AS kind, r.key, NULL::date AS as_of_date
                      FROM runs r WHERE r.stop = 'infinity'
                    UNION ALL
                    SELECT t.sid, t.known_at, 1, NULL, t.as_of_date FROM px t) e) s
     WHERE s.kind = 1
),
picked AS MATERIALIZED (
    -- The basis: that, or a later candidate whose run ends but covers known_at;
    -- none if it ended more than 730 days before known_at.
    SELECT t.*, k.key / 2 AS fd_days,
           CASE WHEN k.key % 2 = 1 THEN 'ttm' ELSE 'annual' END AS basis
      FROM px t
      JOIN swept s ON s.sid = t.sid AND s.as_of_date = t.as_of_date
      LEFT JOIN (SELECT t2.sid, t2.as_of_date, max(r.key) AS key
                   FROM px t2
                   JOIN runs r ON r.sid = t2.sid AND r.stop < 'infinity'
                              AND r.start <= t2.known_at AND t2.known_at < r.stop
                  GROUP BY t2.sid, t2.as_of_date) o
        ON o.sid = t.sid AND o.as_of_date = t.as_of_date
      CROSS JOIN LATERAL (SELECT greatest(s.key, o.key) AS key) k
     WHERE DATE '1900-01-01' + k.key / 2 >= t.known_at - 730
),
based AS MATERIALIZED (
    SELECT p.sid AS security_id, p.as_of_date, p.known_at,
           p.price_date, p.close_raw, adj.close AS close_adjusted,
           f.basis, f.fd AS fiscal_date, f.seq,
           f.fiscal_year,
           f.fiscal_period,
           f.reported_currency,
           f.known_from,
           f.knowledge_source,
           f.income_known_from,
           f.balance_sheet_known_from,
           f.ttm_quarters_used,
           f.revenue,
           f.cost_of_revenue,
           f.gross_profit,
           f.operating_income,
           f.ebit,
           f.income_tax_expense,
           f.interest_expense,
           f.net_income,
           f.net_income_common,
           f.weighted_average_shares,
           f.net_cash_operating,
           f.depreciation_amortization,
           f.capital_expenditure,
           f.dividends_paid,
           f.net_equity_issuance,
           f.total_assets,
           f.total_current_assets,
           f.total_current_liabilities,
           f.inventory,
           f.cash_and_equivalents,
           f.total_debt,
           f.debt_noncurrent,
           f.total_liabilities,
           f.total_equity_parent,
           f.retained_earnings,
           f.shares_outstanding,
           f.share_factor  -- 0043
      FROM picked p
      JOIN frame f ON f.sid = p.sid AND f.basis = p.basis
                  AND f.fd = DATE '1900-01-01' + p.fd_days
                  AND f.b <= p.known_at AND p.known_at < f.e
      LEFT JOIN LATERAL (
            -- The bar already found, adjusted: one lookup, not the whole view.
            SELECT a.close FROM mart.v_daily_price_adjusted a
             WHERE a.security_id = p.sid AND a.trade_date = p.price_date
             LIMIT 1) adj ON true
),
splits AS MATERIALIZED (
    -- The warehouse's splits, per security: few, and most securities have none.
    SELECT a.security_id AS sid,
           array_agg(a.ex_date ORDER BY a.ex_date) AS ex,
           array_agg(ln(a.split_numerator / a.split_denominator) ORDER BY a.ex_date) AS l
      FROM mart.v_corporate_action a
     WHERE a.action_type = 'split' AND a.split_numerator > 0 AND a.split_denominator > 0
       AND a.split_kind = 'shares'  -- 0048: a spin factor changes no count
       AND a.security_id IN (SELECT sid FROM secs)
     GROUP BY a.security_id
),
p4k AS MATERIALIZED (
    -- The same basis 1 fiscal year earlier, as known then:
    -- labelled 4 quarters back, ended within 730 days before the basis.
    SELECT DISTINCT ON (c.security_id, c.as_of_date)
           c.security_id, c.as_of_date, f.basis, f.fd, f.b
      FROM based c
      JOIN frame f ON f.sid = c.security_id AND f.basis = c.basis AND f.seq = c.seq - 4
                  AND f.cand AND f.b <= c.known_at AND c.known_at < f.e
                  AND f.fd < c.fiscal_date AND f.fd >= c.fiscal_date - 730
     ORDER BY c.security_id, c.as_of_date, f.fd DESC
),
p12k AS MATERIALIZED (
    -- The same basis 3 fiscal years earlier, as known then:
    -- labelled 12 quarters back, ended within 1826 days before the basis.
    SELECT DISTINCT ON (c.security_id, c.as_of_date)
           c.security_id, c.as_of_date, f.basis, f.fd, f.b
      FROM based c
      JOIN frame f ON f.sid = c.security_id AND f.basis = c.basis AND f.seq = c.seq - 12
                  AND f.cand AND f.b <= c.known_at AND c.known_at < f.e
                  AND f.fd < c.fiscal_date AND f.fd >= c.fiscal_date - 1826
     ORDER BY c.security_id, c.as_of_date, f.fd DESC
),
x AS NOT MATERIALIZED (
    SELECT c.*,
           p.income_known_from AS p4_income_known_from,
           p.balance_sheet_known_from AS p4_balance_sheet_known_from,
           p.revenue AS p4_revenue,
           p.cost_of_revenue AS p4_cost_of_revenue,
           p.gross_profit AS p4_gross_profit,
           p.operating_income AS p4_operating_income,
           p.ebit AS p4_ebit,
           p.income_tax_expense AS p4_income_tax_expense,
           p.interest_expense AS p4_interest_expense,
           p.net_income AS p4_net_income,
           p.net_income_common AS p4_net_income_common,
           p.weighted_average_shares AS p4_weighted_average_shares,
           p.net_cash_operating AS p4_net_cash_operating,
           p.depreciation_amortization AS p4_depreciation_amortization,
           p.capital_expenditure AS p4_capital_expenditure,
           p.dividends_paid AS p4_dividends_paid,
           p.net_equity_issuance AS p4_net_equity_issuance,
           p.total_assets AS p4_total_assets,
           p.total_current_assets AS p4_total_current_assets,
           p.total_current_liabilities AS p4_total_current_liabilities,
           p.inventory AS p4_inventory,
           p.cash_and_equivalents AS p4_cash_and_equivalents,
           p.total_debt AS p4_total_debt,
           p.debt_noncurrent AS p4_debt_noncurrent,
           p.total_liabilities AS p4_total_liabilities,
           p.total_equity_parent AS p4_total_equity_parent,
           p.retained_earnings AS p4_retained_earnings,
           p.shares_outstanding AS p4_shares_outstanding,
           p.share_factor AS p4_share_factor,  -- 0043
           q.revenue AS p12_revenue,
           sp.exchange_code, sp.sector_name, sp.industry_name,
           coalesce(sp.currency, 'USD') AS trading_currency,
           round(c.shares_outstanding
                 * (CASE WHEN sp2.ex IS NULL THEN 1 ELSE (SELECT exp(coalesce(sum(u.l), 0))
                   FROM unnest(sp2.ex, sp2.l) AS u(e, l)
                  WHERE u.e > c.balance_sheet_known_from AND u.e <= c.as_of_date) END)) AS so_as_of,
           round(p.shares_outstanding
                 * (CASE WHEN sp2.ex IS NULL THEN 1 ELSE (SELECT exp(coalesce(sum(u.l), 0))
                   FROM unnest(sp2.ex, sp2.l) AS u(e, l)
                  WHERE u.e > p.balance_sheet_known_from AND u.e <= c.as_of_date) END)) AS p4_so_as_of,
           c.weighted_average_shares
               * (CASE WHEN sp2.ex IS NULL THEN 1 ELSE (SELECT exp(coalesce(sum(u.l), 0))
                   FROM unnest(sp2.ex, sp2.l) AS u(e, l)
                  WHERE u.e > c.income_known_from AND u.e <= c.as_of_date) END) AS wa_as_of,
           p.weighted_average_shares
               * (CASE WHEN sp2.ex IS NULL THEN 1 ELSE (SELECT exp(coalesce(sum(u.l), 0))
                   FROM unnest(sp2.ex, sp2.l) AS u(e, l)
                  WHERE u.e > p.income_known_from AND u.e <= c.as_of_date) END) AS p4_wa_as_of,
           dv.per_share AS dividends_per_share_ttm
      FROM based c
      LEFT JOIN p4k k4 ON k4.security_id = c.security_id AND k4.as_of_date = c.as_of_date
      LEFT JOIN frame p ON p.sid = k4.security_id AND p.basis = k4.basis
                       AND p.fd = k4.fd AND p.b = k4.b
      LEFT JOIN p12k k12 ON k12.security_id = c.security_id
                        AND k12.as_of_date = c.as_of_date
      LEFT JOIN frame q ON q.sid = k12.security_id AND q.basis = k12.basis
                       AND q.fd = k12.fd AND q.b = k12.b
      LEFT JOIN mart.v_security_profile sp ON sp.security_id = c.security_id
      LEFT JOIN splits sp2 ON sp2.sid = c.security_id
      CROSS JOIN LATERAL (
            -- Dividends as declared, put in as_of_date's share terms (ADR 0001, 0019).
            SELECT coalesce(sum(a.dividend_amount / (CASE WHEN sp2.ex IS NULL THEN 1 ELSE (SELECT exp(coalesce(sum(u.l), 0))
                   FROM unnest(sp2.ex, sp2.l) AS u(e, l)
                  WHERE u.e > a.ex_date AND u.e <= c.as_of_date) END)), 0) AS per_share
              FROM mart.v_corporate_action a
             WHERE a.security_id = c.security_id AND a.action_type = 'dividend'
               AND a.ex_date > c.as_of_date - 365 AND a.ex_date <= c.as_of_date) dv
),
y AS NOT MATERIALIZED (
    SELECT x.*,
           x.reported_currency IS DISTINCT FROM x.trading_currency AS mismatch,
           -- 0043: SF1's share factor turns the count into the traded line's (an
           -- ADR's ratio, a multi-class issuer's primary class); 1 where none.
           x.close_raw * x.so_as_of * coalesce(x.share_factor, 1) AS mcap,
           x.so_as_of * coalesce(x.share_factor, 1) AS so_traded,
           x.p4_so_as_of * coalesce(x.p4_share_factor, 1) AS p4_so_traded,
           x.ebit + x.depreciation_amortization AS ebitda,
           x.net_cash_operating + x.capital_expenditure AS fcf,
           (x.total_assets + x.p4_total_assets) / 2 AS avg_assets,
           (x.total_equity_parent + x.p4_total_equity_parent) / 2 AS avg_equity,
           -- 0043: per traded share, as the price is.
           x.net_income_common
               / NULLIF(x.wa_as_of * coalesce(x.share_factor, 1), 0) AS eps,
           x.p4_net_income_common
               / NULLIF(x.p4_wa_as_of * coalesce(x.p4_share_factor, 1), 0) AS p4_eps
      FROM x
),
z AS NOT MATERIALIZED (
    SELECT y.*,
           CASE WHEN NOT y.mismatch THEN y.mcap END AS mcx,
           CASE WHEN NOT y.mismatch
                THEN y.mcap + y.total_debt - y.cash_and_equivalents END AS evx
      FROM y
)
SELECT
    z.security_id,
    z.as_of_date,
    z.exchange_code,
    z.sector_name,
    z.industry_name,
    z.trading_currency,
    z.reported_currency,
    z.mismatch,
    z.basis,
    z.fiscal_date,
    z.fiscal_year,
    z.fiscal_period,
    z.known_from,
    z.knowledge_source,
    z.as_of_date - z.known_from,
    z.ttm_quarters_used,
    z.price_date,
    z.close_raw,
    z.close_adjusted,
    z.so_as_of,
    z.mcap,
    z.evx,
    -- value: yields, fundamental / price
    mart.fn_ratio(z.net_income_common, z.mcx),
    CASE WHEN z.total_equity_parent > 0 THEN mart.fn_ratio(z.total_equity_parent, z.mcx) END,
    mart.fn_ratio(z.revenue, z.mcx),
    mart.fn_ratio(z.net_cash_operating, z.mcx),
    mart.fn_ratio(z.fcf, z.mcx),
    mart.fn_ratio(z.evx, z.ebitda),
    mart.fn_ratio(z.evx, z.revenue),
    mart.fn_ratio(z.evx, z.ebit),
    mart.fn_ratio(z.evx, z.fcf),
    -- quality
    mart.fn_ratio(z.net_income_common, z.avg_equity),
    mart.fn_ratio(z.net_income, z.avg_assets),
    mart.fn_ratio(z.ebit - z.income_tax_expense,
                  z.total_debt + z.total_equity_parent - z.cash_and_equivalents),
    mart.fn_ratio(z.gross_profit, z.total_assets),
    mart.fn_ratio(z.gross_profit, z.revenue),
    mart.fn_ratio(z.operating_income, z.revenue),
    mart.fn_ratio(z.net_income, z.revenue),
    mart.fn_ratio(z.revenue, z.avg_assets),
    mart.fn_ratio(z.net_income - z.net_cash_operating, z.avg_assets),
    mart.fn_ratio(z.total_debt + z.total_equity_parent - z.cash_and_equivalents,
                  z.p4_total_assets),
    -- safety and leverage
    mart.fn_ratio(z.total_debt, z.total_equity_parent),
    mart.fn_ratio(z.total_debt - z.cash_and_equivalents, z.ebitda),
    mart.fn_ratio(z.ebit, z.interest_expense),
    mart.fn_ratio(z.total_current_assets, z.total_current_liabilities),
    mart.fn_ratio(z.total_current_assets - z.inventory, z.total_current_liabilities),
    CASE WHEN z.total_assets > 0 AND z.total_liabilities > 0 AND NOT z.mismatch THEN
        (1.2 * (z.total_current_assets - z.total_current_liabilities) / z.total_assets
         + 1.4 * z.retained_earnings / z.total_assets
         + 3.3 * z.ebit / z.total_assets
         + 0.6 * z.mcap / z.total_liabilities
         + 1.0 * z.revenue / z.total_assets)::double precision END,
    CASE WHEN z.total_assets > 0 AND z.p4_total_assets > 0
              AND z.total_current_liabilities > 0 AND z.p4_total_current_liabilities > 0
              AND z.revenue > 0 AND z.p4_revenue > 0 THEN
        (z.net_income > 0)::int
        + (z.net_cash_operating > 0)::int
        + (z.net_income / z.total_assets > z.p4_net_income / z.p4_total_assets)::int
        + (z.net_cash_operating > z.net_income)::int
        + (z.debt_noncurrent / z.total_assets
           < z.p4_debt_noncurrent / z.p4_total_assets)::int
        + (z.total_current_assets / z.total_current_liabilities
           > z.p4_total_current_assets / z.p4_total_current_liabilities)::int
        + (z.so_traded <= z.p4_so_traded)::int  -- 0043: in traded units
        + (z.gross_profit / z.revenue > z.p4_gross_profit / z.p4_revenue)::int
        + (z.revenue / z.total_assets > z.p4_revenue / z.p4_total_assets)::int
    END,
    -- growth, every period as known at the knowledge date
    mart.fn_growth(z.revenue, z.p4_revenue),
    mart.fn_growth(z.eps, z.p4_eps),
    CASE WHEN z.revenue > 0 AND z.p12_revenue > 0
         THEN (power(z.revenue / z.p12_revenue, 1.0 / 3) - 1)::double precision END,
    mart.fn_growth(z.net_income, z.p4_net_income),
    -- investment and issuance
    mart.fn_growth(z.total_assets, z.p4_total_assets),
    mart.fn_ratio(-z.capital_expenditure, z.revenue),
    -- 0043: in traded units, so a class conversion (BRK's A into B) is not issuance.
    CASE WHEN z.so_traded > 0 AND z.p4_so_traded > 0
         THEN ln(z.so_traded / z.p4_so_traded)::double precision END,
    -- payout
    mart.fn_ratio(z.dividends_per_share_ttm, z.close_raw),
    mart.fn_ratio(-z.net_equity_issuance, z.mcx),
    mart.fn_ratio(-z.dividends_paid - z.net_equity_issuance, z.mcx),
    -- 0043: the factor applied; NULL with no count.
    CASE WHEN z.so_as_of IS NOT NULL THEN coalesce(z.share_factor, 1) END
  FROM z
$fn$;

COMMENT ON FUNCTION mart.fn_fundamental_panel(date[], integer) IS
    'The factor panel at any dates: per security that traded in the week to each, '
    'the factors from what was known at that date - extra_lag_days (plan §5.3, '
    'ADR 0019). Every factor is defined here; the snapshot and '
    'mart.fundamental_panel select it. Market cap, EV and the per-share and '
    'issuance measures carry SF1''s share factor (0043).'
    ' Share counts and dividends per share follow share splits only: a '
    'spin factor is none (0048).';

-- ---------------------------------------------------------------------------
-- Privileges (repeated, as 0042 and 0043)
-- ---------------------------------------------------------------------------
GRANT SELECT ON mart.v_corporate_action, mart.v_security_action_summary,
    mart.v_security_attributes TO fafnir_read, fafnir_app, fafnir_ops;
REVOKE ALL ON FUNCTION mart.fn_daily_market_cap(bigint[], date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mart.fn_daily_market_cap(bigint[], date, date)
    TO fafnir_read, fafnir_app, fafnir_ops;
REVOKE ALL ON FUNCTION mart.fn_fundamental_panel(date[], integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mart.fn_fundamental_panel(date[], integer)
    TO fafnir_read, fafnir_app, fafnir_ops;

COMMIT;
