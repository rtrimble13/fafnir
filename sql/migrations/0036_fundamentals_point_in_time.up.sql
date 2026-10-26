-- 0036_fundamentals_point_in_time.up.sql
-- Point-in-time access to the statements, and TTM (#95; ADR 0018, ADR 0019; plan
-- doc/plans/fundamentals-sf1.md §5.1-5.2).
--
-- GENERATED from fafnir.ingest.fundamentals_map, like 0035: a test holds the
-- columns of every object here equal to the map.
--
-- What it adds, all in mart (ADR 0009: mart is the read seam):
--
--   mart.v_income_statement_versions, mart.v_balance_sheet_versions,
--   mart.v_cash_flow_statement_versions
--       Every version of every period, on both timelines. Definer-rights views, so
--       a mart reader reaches the statements without core. They leave out what mart
--       must not expose: extra (SF1's vendor metrics, never read by mart: ADR
--       0019), the vendor key (as 0034 leaves it out), the content hash, and
--       lineage.
--
--   mart.fn_statements_as_of(as_of date)
--       Per (security_id, period_type), the latest period with a version in force
--       at as_of, its three statements joined on fiscal_date (never on knowledge
--       dates), each at its own version in force. A statement missing for the
--       period is NULL, with has_income / has_balance_sheet / has_cash_flow.
--       known_from is the latest of the three: the row was not knowable before it.
--       knowledge_source is the weakest of the three (estimated, then observed,
--       then filed), so a reader can exclude estimated knowledge dates.
--
--   mart.fn_statements_as_of(as_of date, load_as_of timestamptz)
--       The same, as fafnir held the statements at load_as_of: rows loaded by then
--       and not yet superseded, with a version loaded later ignored and its
--       predecessor's interval read as open (§4.6). Reproduces a past run.
--
--   mart.fn_fundamentals_ttm(as_of date)
--       Trailing twelve months for every quarter-end of the income statement in
--       force at as_of. Flows (item_class 'flow') sum four contiguous quarters, or
--       are NULL: never a partial sum. Contiguous: consecutive fiscal labels, each
--       period end 8 to 18 weeks after the one before (a 16-week first quarter
--       included). A flow NULL in any of the four is NULL. Stocks and share counts
--       take the latest quarter: a balance-sheet item is never summed. SF1 files Q4
--       and its quarters are discrete (#83), so there is no derived Q4 and no
--       year-to-date de-cumulation. known_from is the latest of every constituent:
--       a TTM figure is not knowable before its last quarter is. Share counts are in
--       the terms of income_known_from and balance_sheet_known_from (ADR 0019).
--
--   mart.v_statements_current, mart.v_fundamentals_ttm
--       The two functions as of today: for screening, not backtests.
--
-- The functions are plain SQL, STABLE, and run with their caller's rights. They read
-- only the definer-rights views above, so a caller needs mart and EXECUTE, never
-- core, and the planner can inline them: a caller's filter on security_id reaches
-- the statement tables' indexes. (A SECURITY DEFINER function is never inlined.)
--
-- Privileges. SELECT on new mart relations comes from the default privileges (0001,
-- 0021) for fafnir_read, fafnir_app and fafnir_ops; the grants below repeat them, as
-- 0035 did. Default privileges do not cover functions: PostgreSQL grants EXECUTE to
-- PUBLIC. That is revoked here, and EXECUTE granted to the three read roles only.
--
-- Empty on arrival in effect: nothing here writes a row.

BEGIN;

-- ---------------------------------------------------------------------------
-- mart.v_income_statement_versions
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_income_statement_versions AS
SELECT
    t.security_id,
    t.period_type,
    t.fiscal_date,
    t.calendar_date,
    t.fiscal_year,
    t.fiscal_period,
    t.version_no,
    t.known_from,
    t.known_to,
    t.knowledge_source,
    t.filing_date,
    t.loaded_at,
    t.superseded_at,
    t.reported_currency,
    t.revenue,
    t.cost_of_revenue,
    t.gross_profit,
    t.research_and_development,
    t.selling_general_and_administrative,
    t.operating_expenses,
    t.operating_income,
    t.interest_expense,
    t.ebit,
    t.income_before_tax,
    t.income_tax_expense,
    t.consolidated_income,
    t.net_income_noncontrolling,
    t.net_income_discontinued,
    t.net_income,
    t.preferred_dividends,
    t.net_income_common,
    t.weighted_average_shares,
    t.weighted_average_shares_diluted
FROM core.income_statement t;

COMMENT ON VIEW mart.v_income_statement_versions IS
    'Every version of every period of core.income_statement, on both timelines (ADR 0018). '
    'In force at D: superseded_at IS NULL AND known_from <= D AND (known_to IS NULL '
    'OR D < known_to). Share counts are in the share terms of known_from (ADR 0019). '
    'Read mart.fn_statements_as_of for a point-in-time cross-section.';

-- ---------------------------------------------------------------------------
-- mart.v_balance_sheet_versions
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_balance_sheet_versions AS
SELECT
    t.security_id,
    t.period_type,
    t.fiscal_date,
    t.calendar_date,
    t.fiscal_year,
    t.fiscal_period,
    t.version_no,
    t.known_from,
    t.known_to,
    t.knowledge_source,
    t.filing_date,
    t.loaded_at,
    t.superseded_at,
    t.reported_currency,
    t.cash_and_equivalents,
    t.investments_current,
    t.investments_noncurrent,
    t.investments_total,
    t.receivables,
    t.inventory,
    t.tax_assets,
    t.total_current_assets,
    t.property_plant_equipment_net,
    t.goodwill_and_intangibles,
    t.total_noncurrent_assets,
    t.total_assets,
    t.payables,
    t.deferred_revenue,
    t.deposit_liabilities,
    t.debt_current,
    t.debt_noncurrent,
    t.total_debt,
    t.tax_liabilities,
    t.total_current_liabilities,
    t.total_noncurrent_liabilities,
    t.total_liabilities,
    t.accumulated_other_comprehensive_income,
    t.retained_earnings,
    t.total_equity_parent,
    t.shares_outstanding
FROM core.balance_sheet t;

COMMENT ON VIEW mart.v_balance_sheet_versions IS
    'Every version of every period of core.balance_sheet, on both timelines (ADR 0018). '
    'In force at D: superseded_at IS NULL AND known_from <= D AND (known_to IS NULL '
    'OR D < known_to). Share counts are in the share terms of known_from (ADR 0019). '
    'Read mart.fn_statements_as_of for a point-in-time cross-section.';

-- ---------------------------------------------------------------------------
-- mart.v_cash_flow_statement_versions
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_cash_flow_statement_versions AS
SELECT
    t.security_id,
    t.period_type,
    t.fiscal_date,
    t.calendar_date,
    t.fiscal_year,
    t.fiscal_period,
    t.version_no,
    t.known_from,
    t.known_to,
    t.knowledge_source,
    t.filing_date,
    t.loaded_at,
    t.superseded_at,
    t.reported_currency,
    t.net_cash_operating,
    t.depreciation_amortization,
    t.share_based_compensation,
    t.capital_expenditure,
    t.net_cash_business_acquisitions,
    t.net_cash_investment_acquisitions,
    t.net_cash_investing,
    t.net_debt_issuance,
    t.net_equity_issuance,
    t.dividends_paid,
    t.net_cash_financing,
    t.fx_effect_on_cash,
    t.net_change_in_cash
FROM core.cash_flow_statement t;

COMMENT ON VIEW mart.v_cash_flow_statement_versions IS
    'Every version of every period of core.cash_flow_statement, on both timelines (ADR 0018). '
    'In force at D: superseded_at IS NULL AND known_from <= D AND (known_to IS NULL '
    'OR D < known_to). Share counts are in the share terms of known_from (ADR 0019). '
    'Read mart.fn_statements_as_of for a point-in-time cross-section.';

-- ---------------------------------------------------------------------------
-- mart.fn_statements_as_of: one axis, and two
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mart.fn_statements_as_of(as_of date)
RETURNS TABLE (
    security_id bigint,
    period_type text,
    fiscal_date date,
    calendar_date date,
    fiscal_year integer,
    fiscal_period text,
    reported_currency text,
    known_from date,
    knowledge_source text,
    has_income boolean,
    has_balance_sheet boolean,
    has_cash_flow boolean,
    income_version_no integer,
    income_known_from date,
    income_knowledge_source text,
    balance_sheet_version_no integer,
    balance_sheet_known_from date,
    balance_sheet_knowledge_source text,
    cash_flow_version_no integer,
    cash_flow_known_from date,
    cash_flow_knowledge_source text,
    revenue numeric(24,2),
    cost_of_revenue numeric(24,2),
    gross_profit numeric(24,2),
    research_and_development numeric(24,2),
    selling_general_and_administrative numeric(24,2),
    operating_expenses numeric(24,2),
    operating_income numeric(24,2),
    interest_expense numeric(24,2),
    ebit numeric(24,2),
    income_before_tax numeric(24,2),
    income_tax_expense numeric(24,2),
    consolidated_income numeric(24,2),
    net_income_noncontrolling numeric(24,2),
    net_income_discontinued numeric(24,2),
    net_income numeric(24,2),
    preferred_dividends numeric(24,2),
    net_income_common numeric(24,2),
    weighted_average_shares numeric(24,4),
    weighted_average_shares_diluted numeric(24,4),
    cash_and_equivalents numeric(24,2),
    investments_current numeric(24,2),
    investments_noncurrent numeric(24,2),
    investments_total numeric(24,2),
    receivables numeric(24,2),
    inventory numeric(24,2),
    tax_assets numeric(24,2),
    total_current_assets numeric(24,2),
    property_plant_equipment_net numeric(24,2),
    goodwill_and_intangibles numeric(24,2),
    total_noncurrent_assets numeric(24,2),
    total_assets numeric(24,2),
    payables numeric(24,2),
    deferred_revenue numeric(24,2),
    deposit_liabilities numeric(24,2),
    debt_current numeric(24,2),
    debt_noncurrent numeric(24,2),
    total_debt numeric(24,2),
    tax_liabilities numeric(24,2),
    total_current_liabilities numeric(24,2),
    total_noncurrent_liabilities numeric(24,2),
    total_liabilities numeric(24,2),
    accumulated_other_comprehensive_income numeric(24,2),
    retained_earnings numeric(24,2),
    total_equity_parent numeric(24,2),
    shares_outstanding numeric(24,4),
    net_cash_operating numeric(24,2),
    depreciation_amortization numeric(24,2),
    share_based_compensation numeric(24,2),
    capital_expenditure numeric(24,2),
    net_cash_business_acquisitions numeric(24,2),
    net_cash_investment_acquisitions numeric(24,2),
    net_cash_investing numeric(24,2),
    net_debt_issuance numeric(24,2),
    net_equity_issuance numeric(24,2),
    dividends_paid numeric(24,2),
    net_cash_financing numeric(24,2),
    fx_effect_on_cash numeric(24,2),
    net_change_in_cash numeric(24,2)
)
LANGUAGE sql STABLE PARALLEL SAFE
AS $fn$
WITH
inc AS NOT MATERIALIZED (
    SELECT v.* FROM mart.v_income_statement_versions v
     WHERE v.superseded_at IS NULL AND v.known_from <= as_of
       AND (v.known_to IS NULL OR as_of < v.known_to)
),
bal AS NOT MATERIALIZED (
    SELECT v.* FROM mart.v_balance_sheet_versions v
     WHERE v.superseded_at IS NULL AND v.known_from <= as_of
       AND (v.known_to IS NULL OR as_of < v.known_to)
),
cfs AS NOT MATERIALIZED (
    SELECT v.* FROM mart.v_cash_flow_statement_versions v
     WHERE v.superseded_at IS NULL AND v.known_from <= as_of
       AND (v.known_to IS NULL OR as_of < v.known_to)
),
latest AS NOT MATERIALIZED (
    SELECT DISTINCT ON (p.security_id, p.period_type)
           p.security_id, p.period_type, p.fiscal_date
      FROM (SELECT security_id, period_type, fiscal_date FROM inc
            UNION ALL
            SELECT security_id, period_type, fiscal_date FROM bal
            UNION ALL
            SELECT security_id, period_type, fiscal_date FROM cfs) p
     ORDER BY p.security_id, p.period_type, p.fiscal_date DESC
)
SELECT l.security_id,
       l.period_type,
       l.fiscal_date,
       coalesce(i.calendar_date, b.calendar_date, c.calendar_date),
       coalesce(i.fiscal_year, b.fiscal_year, c.fiscal_year),
       coalesce(i.fiscal_period, b.fiscal_period, c.fiscal_period),
       coalesce(i.reported_currency, b.reported_currency, c.reported_currency),
       greatest(i.known_from, b.known_from, c.known_from),
       CASE WHEN 'estimated' = ANY(ARRAY[i.knowledge_source, b.knowledge_source, c.knowledge_source]) THEN 'estimated'
            WHEN 'observed' = ANY(ARRAY[i.knowledge_source, b.knowledge_source, c.knowledge_source]) THEN 'observed'
            ELSE 'filed' END,
       i.security_id IS NOT NULL,
       b.security_id IS NOT NULL,
       c.security_id IS NOT NULL,
       i.version_no,
       i.known_from,
       i.knowledge_source,
       b.version_no,
       b.known_from,
       b.knowledge_source,
       c.version_no,
       c.known_from,
       c.knowledge_source,
       i.revenue,
       i.cost_of_revenue,
       i.gross_profit,
       i.research_and_development,
       i.selling_general_and_administrative,
       i.operating_expenses,
       i.operating_income,
       i.interest_expense,
       i.ebit,
       i.income_before_tax,
       i.income_tax_expense,
       i.consolidated_income,
       i.net_income_noncontrolling,
       i.net_income_discontinued,
       i.net_income,
       i.preferred_dividends,
       i.net_income_common,
       i.weighted_average_shares,
       i.weighted_average_shares_diluted,
       b.cash_and_equivalents,
       b.investments_current,
       b.investments_noncurrent,
       b.investments_total,
       b.receivables,
       b.inventory,
       b.tax_assets,
       b.total_current_assets,
       b.property_plant_equipment_net,
       b.goodwill_and_intangibles,
       b.total_noncurrent_assets,
       b.total_assets,
       b.payables,
       b.deferred_revenue,
       b.deposit_liabilities,
       b.debt_current,
       b.debt_noncurrent,
       b.total_debt,
       b.tax_liabilities,
       b.total_current_liabilities,
       b.total_noncurrent_liabilities,
       b.total_liabilities,
       b.accumulated_other_comprehensive_income,
       b.retained_earnings,
       b.total_equity_parent,
       b.shares_outstanding,
       c.net_cash_operating,
       c.depreciation_amortization,
       c.share_based_compensation,
       c.capital_expenditure,
       c.net_cash_business_acquisitions,
       c.net_cash_investment_acquisitions,
       c.net_cash_investing,
       c.net_debt_issuance,
       c.net_equity_issuance,
       c.dividends_paid,
       c.net_cash_financing,
       c.fx_effect_on_cash,
       c.net_change_in_cash
  FROM latest l
  LEFT JOIN inc i ON i.security_id = l.security_id AND i.period_type = l.period_type AND i.fiscal_date = l.fiscal_date
  LEFT JOIN bal b ON b.security_id = l.security_id AND b.period_type = l.period_type AND b.fiscal_date = l.fiscal_date
  LEFT JOIN cfs c ON c.security_id = l.security_id AND c.period_type = l.period_type AND c.fiscal_date = l.fiscal_date
$fn$;

COMMENT ON FUNCTION mart.fn_statements_as_of(date) IS
    'Per (security_id, period_type), the latest period with a version in force at '
    'as_of, the three statements joined on fiscal_date, each at its own version in '
    'force (ADR 0018). For backtests: nothing in it was knowable after as_of.';

CREATE OR REPLACE FUNCTION mart.fn_statements_as_of(as_of date, load_as_of timestamptz)
RETURNS TABLE (
    security_id bigint,
    period_type text,
    fiscal_date date,
    calendar_date date,
    fiscal_year integer,
    fiscal_period text,
    reported_currency text,
    known_from date,
    knowledge_source text,
    has_income boolean,
    has_balance_sheet boolean,
    has_cash_flow boolean,
    income_version_no integer,
    income_known_from date,
    income_knowledge_source text,
    balance_sheet_version_no integer,
    balance_sheet_known_from date,
    balance_sheet_knowledge_source text,
    cash_flow_version_no integer,
    cash_flow_known_from date,
    cash_flow_knowledge_source text,
    revenue numeric(24,2),
    cost_of_revenue numeric(24,2),
    gross_profit numeric(24,2),
    research_and_development numeric(24,2),
    selling_general_and_administrative numeric(24,2),
    operating_expenses numeric(24,2),
    operating_income numeric(24,2),
    interest_expense numeric(24,2),
    ebit numeric(24,2),
    income_before_tax numeric(24,2),
    income_tax_expense numeric(24,2),
    consolidated_income numeric(24,2),
    net_income_noncontrolling numeric(24,2),
    net_income_discontinued numeric(24,2),
    net_income numeric(24,2),
    preferred_dividends numeric(24,2),
    net_income_common numeric(24,2),
    weighted_average_shares numeric(24,4),
    weighted_average_shares_diluted numeric(24,4),
    cash_and_equivalents numeric(24,2),
    investments_current numeric(24,2),
    investments_noncurrent numeric(24,2),
    investments_total numeric(24,2),
    receivables numeric(24,2),
    inventory numeric(24,2),
    tax_assets numeric(24,2),
    total_current_assets numeric(24,2),
    property_plant_equipment_net numeric(24,2),
    goodwill_and_intangibles numeric(24,2),
    total_noncurrent_assets numeric(24,2),
    total_assets numeric(24,2),
    payables numeric(24,2),
    deferred_revenue numeric(24,2),
    deposit_liabilities numeric(24,2),
    debt_current numeric(24,2),
    debt_noncurrent numeric(24,2),
    total_debt numeric(24,2),
    tax_liabilities numeric(24,2),
    total_current_liabilities numeric(24,2),
    total_noncurrent_liabilities numeric(24,2),
    total_liabilities numeric(24,2),
    accumulated_other_comprehensive_income numeric(24,2),
    retained_earnings numeric(24,2),
    total_equity_parent numeric(24,2),
    shares_outstanding numeric(24,4),
    net_cash_operating numeric(24,2),
    depreciation_amortization numeric(24,2),
    share_based_compensation numeric(24,2),
    capital_expenditure numeric(24,2),
    net_cash_business_acquisitions numeric(24,2),
    net_cash_investment_acquisitions numeric(24,2),
    net_cash_investing numeric(24,2),
    net_debt_issuance numeric(24,2),
    net_equity_issuance numeric(24,2),
    dividends_paid numeric(24,2),
    net_cash_financing numeric(24,2),
    fx_effect_on_cash numeric(24,2),
    net_change_in_cash numeric(24,2)
)
LANGUAGE sql STABLE PARALLEL SAFE
AS $fn$
WITH
inc AS NOT MATERIALIZED (
    SELECT h.* FROM (
        SELECT v.*, lead(v.known_from) OVER (
                   PARTITION BY v.security_id, v.period_type, v.fiscal_date
                   ORDER BY v.version_no) AS held_known_to
          FROM mart.v_income_statement_versions v
         WHERE v.loaded_at <= load_as_of
           AND (v.superseded_at IS NULL OR v.superseded_at > load_as_of)
    ) h
     WHERE h.known_from <= as_of
       AND (h.held_known_to IS NULL OR as_of < h.held_known_to)
),
bal AS NOT MATERIALIZED (
    SELECT h.* FROM (
        SELECT v.*, lead(v.known_from) OVER (
                   PARTITION BY v.security_id, v.period_type, v.fiscal_date
                   ORDER BY v.version_no) AS held_known_to
          FROM mart.v_balance_sheet_versions v
         WHERE v.loaded_at <= load_as_of
           AND (v.superseded_at IS NULL OR v.superseded_at > load_as_of)
    ) h
     WHERE h.known_from <= as_of
       AND (h.held_known_to IS NULL OR as_of < h.held_known_to)
),
cfs AS NOT MATERIALIZED (
    SELECT h.* FROM (
        SELECT v.*, lead(v.known_from) OVER (
                   PARTITION BY v.security_id, v.period_type, v.fiscal_date
                   ORDER BY v.version_no) AS held_known_to
          FROM mart.v_cash_flow_statement_versions v
         WHERE v.loaded_at <= load_as_of
           AND (v.superseded_at IS NULL OR v.superseded_at > load_as_of)
    ) h
     WHERE h.known_from <= as_of
       AND (h.held_known_to IS NULL OR as_of < h.held_known_to)
),
latest AS NOT MATERIALIZED (
    SELECT DISTINCT ON (p.security_id, p.period_type)
           p.security_id, p.period_type, p.fiscal_date
      FROM (SELECT security_id, period_type, fiscal_date FROM inc
            UNION ALL
            SELECT security_id, period_type, fiscal_date FROM bal
            UNION ALL
            SELECT security_id, period_type, fiscal_date FROM cfs) p
     ORDER BY p.security_id, p.period_type, p.fiscal_date DESC
)
SELECT l.security_id,
       l.period_type,
       l.fiscal_date,
       coalesce(i.calendar_date, b.calendar_date, c.calendar_date),
       coalesce(i.fiscal_year, b.fiscal_year, c.fiscal_year),
       coalesce(i.fiscal_period, b.fiscal_period, c.fiscal_period),
       coalesce(i.reported_currency, b.reported_currency, c.reported_currency),
       greatest(i.known_from, b.known_from, c.known_from),
       CASE WHEN 'estimated' = ANY(ARRAY[i.knowledge_source, b.knowledge_source, c.knowledge_source]) THEN 'estimated'
            WHEN 'observed' = ANY(ARRAY[i.knowledge_source, b.knowledge_source, c.knowledge_source]) THEN 'observed'
            ELSE 'filed' END,
       i.security_id IS NOT NULL,
       b.security_id IS NOT NULL,
       c.security_id IS NOT NULL,
       i.version_no,
       i.known_from,
       i.knowledge_source,
       b.version_no,
       b.known_from,
       b.knowledge_source,
       c.version_no,
       c.known_from,
       c.knowledge_source,
       i.revenue,
       i.cost_of_revenue,
       i.gross_profit,
       i.research_and_development,
       i.selling_general_and_administrative,
       i.operating_expenses,
       i.operating_income,
       i.interest_expense,
       i.ebit,
       i.income_before_tax,
       i.income_tax_expense,
       i.consolidated_income,
       i.net_income_noncontrolling,
       i.net_income_discontinued,
       i.net_income,
       i.preferred_dividends,
       i.net_income_common,
       i.weighted_average_shares,
       i.weighted_average_shares_diluted,
       b.cash_and_equivalents,
       b.investments_current,
       b.investments_noncurrent,
       b.investments_total,
       b.receivables,
       b.inventory,
       b.tax_assets,
       b.total_current_assets,
       b.property_plant_equipment_net,
       b.goodwill_and_intangibles,
       b.total_noncurrent_assets,
       b.total_assets,
       b.payables,
       b.deferred_revenue,
       b.deposit_liabilities,
       b.debt_current,
       b.debt_noncurrent,
       b.total_debt,
       b.tax_liabilities,
       b.total_current_liabilities,
       b.total_noncurrent_liabilities,
       b.total_liabilities,
       b.accumulated_other_comprehensive_income,
       b.retained_earnings,
       b.total_equity_parent,
       b.shares_outstanding,
       c.net_cash_operating,
       c.depreciation_amortization,
       c.share_based_compensation,
       c.capital_expenditure,
       c.net_cash_business_acquisitions,
       c.net_cash_investment_acquisitions,
       c.net_cash_investing,
       c.net_debt_issuance,
       c.net_equity_issuance,
       c.dividends_paid,
       c.net_cash_financing,
       c.fx_effect_on_cash,
       c.net_change_in_cash
  FROM latest l
  LEFT JOIN inc i ON i.security_id = l.security_id AND i.period_type = l.period_type AND i.fiscal_date = l.fiscal_date
  LEFT JOIN bal b ON b.security_id = l.security_id AND b.period_type = l.period_type AND b.fiscal_date = l.fiscal_date
  LEFT JOIN cfs c ON c.security_id = l.security_id AND c.period_type = l.period_type AND c.fiscal_date = l.fiscal_date
$fn$;

COMMENT ON FUNCTION mart.fn_statements_as_of(date, timestamptz) IS
    'fn_statements_as_of(as_of) as fafnir held the statements at load_as_of: '
    'rows loaded by then and not yet superseded; a version loaded later is '
    'ignored and its predecessor read as open. Reproduces a past run.';

-- ---------------------------------------------------------------------------
-- mart.fn_fundamentals_ttm
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mart.fn_fundamentals_ttm(as_of date)
RETURNS TABLE (
    security_id bigint,
    fiscal_date date,
    calendar_date date,
    fiscal_year integer,
    fiscal_period text,
    reported_currency text,
    ttm_quarters_used integer,
    cash_flow_quarters_used integer,
    has_balance_sheet boolean,
    known_from date,
    knowledge_source text,
    income_known_from date,
    balance_sheet_known_from date,
    revenue numeric(24,2),
    cost_of_revenue numeric(24,2),
    gross_profit numeric(24,2),
    research_and_development numeric(24,2),
    selling_general_and_administrative numeric(24,2),
    operating_expenses numeric(24,2),
    operating_income numeric(24,2),
    interest_expense numeric(24,2),
    ebit numeric(24,2),
    income_before_tax numeric(24,2),
    income_tax_expense numeric(24,2),
    consolidated_income numeric(24,2),
    net_income_noncontrolling numeric(24,2),
    net_income_discontinued numeric(24,2),
    net_income numeric(24,2),
    preferred_dividends numeric(24,2),
    net_income_common numeric(24,2),
    weighted_average_shares numeric(24,4),
    weighted_average_shares_diluted numeric(24,4),
    cash_and_equivalents numeric(24,2),
    investments_current numeric(24,2),
    investments_noncurrent numeric(24,2),
    investments_total numeric(24,2),
    receivables numeric(24,2),
    inventory numeric(24,2),
    tax_assets numeric(24,2),
    total_current_assets numeric(24,2),
    property_plant_equipment_net numeric(24,2),
    goodwill_and_intangibles numeric(24,2),
    total_noncurrent_assets numeric(24,2),
    total_assets numeric(24,2),
    payables numeric(24,2),
    deferred_revenue numeric(24,2),
    deposit_liabilities numeric(24,2),
    debt_current numeric(24,2),
    debt_noncurrent numeric(24,2),
    total_debt numeric(24,2),
    tax_liabilities numeric(24,2),
    total_current_liabilities numeric(24,2),
    total_noncurrent_liabilities numeric(24,2),
    total_liabilities numeric(24,2),
    accumulated_other_comprehensive_income numeric(24,2),
    retained_earnings numeric(24,2),
    total_equity_parent numeric(24,2),
    shares_outstanding numeric(24,4),
    net_cash_operating numeric(24,2),
    depreciation_amortization numeric(24,2),
    share_based_compensation numeric(24,2),
    capital_expenditure numeric(24,2),
    net_cash_business_acquisitions numeric(24,2),
    net_cash_investment_acquisitions numeric(24,2),
    net_cash_investing numeric(24,2),
    net_debt_issuance numeric(24,2),
    net_equity_issuance numeric(24,2),
    dividends_paid numeric(24,2),
    net_cash_financing numeric(24,2),
    fx_effect_on_cash numeric(24,2),
    net_change_in_cash numeric(24,2)
)
LANGUAGE sql STABLE PARALLEL SAFE
AS $fn$
WITH
inc AS NOT MATERIALIZED (
    SELECT v.* FROM mart.v_income_statement_versions v
     WHERE v.superseded_at IS NULL AND v.known_from <= as_of
       AND (v.known_to IS NULL OR as_of < v.known_to)
       AND v.period_type = 'quarter'
),
cfs AS NOT MATERIALIZED (
    SELECT v.* FROM mart.v_cash_flow_statement_versions v
     WHERE v.superseded_at IS NULL AND v.known_from <= as_of
       AND (v.known_to IS NULL OR as_of < v.known_to)
       AND v.period_type = 'quarter'
),
bal AS NOT MATERIALIZED (
    SELECT v.* FROM mart.v_balance_sheet_versions v
     WHERE v.superseded_at IS NULL AND v.known_from <= as_of
       AND (v.known_to IS NULL OR as_of < v.known_to)
       AND v.period_type = 'quarter'
),
inc_w AS NOT MATERIALIZED (
    SELECT q.*,
           lag(q.qn, 1) OVER w AS qn1,
           lag(q.fiscal_date, 1) OVER w AS d1,
           lag(q.known_from, 1) OVER w AS k1,
           lag(q.knowledge_source, 1) OVER w AS s1,
           lag(q.qn, 2) OVER w AS qn2,
           lag(q.fiscal_date, 2) OVER w AS d2,
           lag(q.known_from, 2) OVER w AS k2,
           lag(q.knowledge_source, 2) OVER w AS s2,
           lag(q.qn, 3) OVER w AS qn3,
           lag(q.fiscal_date, 3) OVER w AS d3,
           lag(q.known_from, 3) OVER w AS k3,
           lag(q.knowledge_source, 3) OVER w AS s3,
           sum(q.revenue) OVER f AS revenue_sum,
           count(q.revenue) OVER f AS revenue_n,
           sum(q.cost_of_revenue) OVER f AS cost_of_revenue_sum,
           count(q.cost_of_revenue) OVER f AS cost_of_revenue_n,
           sum(q.gross_profit) OVER f AS gross_profit_sum,
           count(q.gross_profit) OVER f AS gross_profit_n,
           sum(q.research_and_development) OVER f AS research_and_development_sum,
           count(q.research_and_development) OVER f AS research_and_development_n,
           sum(q.selling_general_and_administrative) OVER f AS selling_general_and_administrative_sum,
           count(q.selling_general_and_administrative) OVER f AS selling_general_and_administrative_n,
           sum(q.operating_expenses) OVER f AS operating_expenses_sum,
           count(q.operating_expenses) OVER f AS operating_expenses_n,
           sum(q.operating_income) OVER f AS operating_income_sum,
           count(q.operating_income) OVER f AS operating_income_n,
           sum(q.interest_expense) OVER f AS interest_expense_sum,
           count(q.interest_expense) OVER f AS interest_expense_n,
           sum(q.ebit) OVER f AS ebit_sum,
           count(q.ebit) OVER f AS ebit_n,
           sum(q.income_before_tax) OVER f AS income_before_tax_sum,
           count(q.income_before_tax) OVER f AS income_before_tax_n,
           sum(q.income_tax_expense) OVER f AS income_tax_expense_sum,
           count(q.income_tax_expense) OVER f AS income_tax_expense_n,
           sum(q.consolidated_income) OVER f AS consolidated_income_sum,
           count(q.consolidated_income) OVER f AS consolidated_income_n,
           sum(q.net_income_noncontrolling) OVER f AS net_income_noncontrolling_sum,
           count(q.net_income_noncontrolling) OVER f AS net_income_noncontrolling_n,
           sum(q.net_income_discontinued) OVER f AS net_income_discontinued_sum,
           count(q.net_income_discontinued) OVER f AS net_income_discontinued_n,
           sum(q.net_income) OVER f AS net_income_sum,
           count(q.net_income) OVER f AS net_income_n,
           sum(q.preferred_dividends) OVER f AS preferred_dividends_sum,
           count(q.preferred_dividends) OVER f AS preferred_dividends_n,
           sum(q.net_income_common) OVER f AS net_income_common_sum,
           count(q.net_income_common) OVER f AS net_income_common_n
      FROM (SELECT x.*,
                   x.fiscal_year * 4 + substr(x.fiscal_period, 2)::int AS qn
              FROM inc x) q
    WINDOW w AS (PARTITION BY q.security_id ORDER BY q.fiscal_date),
           f AS (w ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)
),
cfs_w AS NOT MATERIALIZED (
    SELECT q.*,
           lag(q.qn, 1) OVER w AS qn1,
           lag(q.fiscal_date, 1) OVER w AS d1,
           lag(q.known_from, 1) OVER w AS k1,
           lag(q.knowledge_source, 1) OVER w AS s1,
           lag(q.qn, 2) OVER w AS qn2,
           lag(q.fiscal_date, 2) OVER w AS d2,
           lag(q.known_from, 2) OVER w AS k2,
           lag(q.knowledge_source, 2) OVER w AS s2,
           lag(q.qn, 3) OVER w AS qn3,
           lag(q.fiscal_date, 3) OVER w AS d3,
           lag(q.known_from, 3) OVER w AS k3,
           lag(q.knowledge_source, 3) OVER w AS s3,
           sum(q.net_cash_operating) OVER f AS net_cash_operating_sum,
           count(q.net_cash_operating) OVER f AS net_cash_operating_n,
           sum(q.depreciation_amortization) OVER f AS depreciation_amortization_sum,
           count(q.depreciation_amortization) OVER f AS depreciation_amortization_n,
           sum(q.share_based_compensation) OVER f AS share_based_compensation_sum,
           count(q.share_based_compensation) OVER f AS share_based_compensation_n,
           sum(q.capital_expenditure) OVER f AS capital_expenditure_sum,
           count(q.capital_expenditure) OVER f AS capital_expenditure_n,
           sum(q.net_cash_business_acquisitions) OVER f AS net_cash_business_acquisitions_sum,
           count(q.net_cash_business_acquisitions) OVER f AS net_cash_business_acquisitions_n,
           sum(q.net_cash_investment_acquisitions) OVER f AS net_cash_investment_acquisitions_sum,
           count(q.net_cash_investment_acquisitions) OVER f AS net_cash_investment_acquisitions_n,
           sum(q.net_cash_investing) OVER f AS net_cash_investing_sum,
           count(q.net_cash_investing) OVER f AS net_cash_investing_n,
           sum(q.net_debt_issuance) OVER f AS net_debt_issuance_sum,
           count(q.net_debt_issuance) OVER f AS net_debt_issuance_n,
           sum(q.net_equity_issuance) OVER f AS net_equity_issuance_sum,
           count(q.net_equity_issuance) OVER f AS net_equity_issuance_n,
           sum(q.dividends_paid) OVER f AS dividends_paid_sum,
           count(q.dividends_paid) OVER f AS dividends_paid_n,
           sum(q.net_cash_financing) OVER f AS net_cash_financing_sum,
           count(q.net_cash_financing) OVER f AS net_cash_financing_n,
           sum(q.fx_effect_on_cash) OVER f AS fx_effect_on_cash_sum,
           count(q.fx_effect_on_cash) OVER f AS fx_effect_on_cash_n,
           sum(q.net_change_in_cash) OVER f AS net_change_in_cash_sum,
           count(q.net_change_in_cash) OVER f AS net_change_in_cash_n
      FROM (SELECT x.*,
                   x.fiscal_year * 4 + substr(x.fiscal_period, 2)::int AS qn
              FROM cfs x) q
    WINDOW w AS (PARTITION BY q.security_id ORDER BY q.fiscal_date),
           f AS (w ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)
),
inc_c AS NOT MATERIALIZED (
    SELECT w.*,
           coalesce((w.qn - w.qn1 = 1 AND w.fiscal_date - w.d1 BETWEEN 56 AND 126), false) AS c1,
           coalesce((w.qn - w.qn1 = 1 AND w.fiscal_date - w.d1 BETWEEN 56 AND 126) AND (w.qn1 - w.qn2 = 1 AND w.d1 - w.d2 BETWEEN 56 AND 126), false) AS c2,
           coalesce((w.qn - w.qn1 = 1 AND w.fiscal_date - w.d1 BETWEEN 56 AND 126) AND (w.qn1 - w.qn2 = 1 AND w.d1 - w.d2 BETWEEN 56 AND 126) AND (w.qn2 - w.qn3 = 1 AND w.d2 - w.d3 BETWEEN 56 AND 126), false) AS c3
      FROM inc_w w
),
cfs_c AS NOT MATERIALIZED (
    SELECT w.*,
           coalesce((w.qn - w.qn1 = 1 AND w.fiscal_date - w.d1 BETWEEN 56 AND 126), false) AS c1,
           coalesce((w.qn - w.qn1 = 1 AND w.fiscal_date - w.d1 BETWEEN 56 AND 126) AND (w.qn1 - w.qn2 = 1 AND w.d1 - w.d2 BETWEEN 56 AND 126), false) AS c2,
           coalesce((w.qn - w.qn1 = 1 AND w.fiscal_date - w.d1 BETWEEN 56 AND 126) AND (w.qn1 - w.qn2 = 1 AND w.d1 - w.d2 BETWEEN 56 AND 126) AND (w.qn2 - w.qn3 = 1 AND w.d2 - w.d3 BETWEEN 56 AND 126), false) AS c3
      FROM cfs_w w
)
SELECT i.security_id,
       i.fiscal_date,
       i.calendar_date,
       i.fiscal_year,
       i.fiscal_period,
       i.reported_currency,
       1 + i.c1::int + i.c2::int + i.c3::int,
       CASE WHEN c.security_id IS NOT NULL THEN 1 + c.c1::int + c.c2::int + c.c3::int END,
       b.security_id IS NOT NULL,
       greatest(i.known_from, CASE WHEN i.c1 THEN i.k1 END, CASE WHEN i.c2 THEN i.k2 END,
                CASE WHEN i.c3 THEN i.k3 END, c.known_from,
                CASE WHEN c.c1 THEN c.k1 END, CASE WHEN c.c2 THEN c.k2 END,
                CASE WHEN c.c3 THEN c.k3 END, b.known_from),
       CASE WHEN 'estimated' = ANY(ARRAY[i.knowledge_source, CASE WHEN i.c1 THEN i.s1 END, CASE WHEN i.c2 THEN i.s2 END, CASE WHEN i.c3 THEN i.s3 END, c.knowledge_source, CASE WHEN c.c1 THEN c.s1 END, CASE WHEN c.c2 THEN c.s2 END, CASE WHEN c.c3 THEN c.s3 END, b.knowledge_source]) THEN 'estimated'
            WHEN 'observed' = ANY(ARRAY[i.knowledge_source, CASE WHEN i.c1 THEN i.s1 END, CASE WHEN i.c2 THEN i.s2 END, CASE WHEN i.c3 THEN i.s3 END, c.knowledge_source, CASE WHEN c.c1 THEN c.s1 END, CASE WHEN c.c2 THEN c.s2 END, CASE WHEN c.c3 THEN c.s3 END, b.knowledge_source]) THEN 'observed'
            ELSE 'filed' END,
       i.known_from,
       b.known_from,
       CASE WHEN i.c3 AND i.revenue_n = 4 THEN i.revenue_sum END,
       CASE WHEN i.c3 AND i.cost_of_revenue_n = 4 THEN i.cost_of_revenue_sum END,
       CASE WHEN i.c3 AND i.gross_profit_n = 4 THEN i.gross_profit_sum END,
       CASE WHEN i.c3 AND i.research_and_development_n = 4 THEN i.research_and_development_sum END,
       CASE WHEN i.c3 AND i.selling_general_and_administrative_n = 4 THEN i.selling_general_and_administrative_sum END,
       CASE WHEN i.c3 AND i.operating_expenses_n = 4 THEN i.operating_expenses_sum END,
       CASE WHEN i.c3 AND i.operating_income_n = 4 THEN i.operating_income_sum END,
       CASE WHEN i.c3 AND i.interest_expense_n = 4 THEN i.interest_expense_sum END,
       CASE WHEN i.c3 AND i.ebit_n = 4 THEN i.ebit_sum END,
       CASE WHEN i.c3 AND i.income_before_tax_n = 4 THEN i.income_before_tax_sum END,
       CASE WHEN i.c3 AND i.income_tax_expense_n = 4 THEN i.income_tax_expense_sum END,
       CASE WHEN i.c3 AND i.consolidated_income_n = 4 THEN i.consolidated_income_sum END,
       CASE WHEN i.c3 AND i.net_income_noncontrolling_n = 4 THEN i.net_income_noncontrolling_sum END,
       CASE WHEN i.c3 AND i.net_income_discontinued_n = 4 THEN i.net_income_discontinued_sum END,
       CASE WHEN i.c3 AND i.net_income_n = 4 THEN i.net_income_sum END,
       CASE WHEN i.c3 AND i.preferred_dividends_n = 4 THEN i.preferred_dividends_sum END,
       CASE WHEN i.c3 AND i.net_income_common_n = 4 THEN i.net_income_common_sum END,
       i.weighted_average_shares,
       i.weighted_average_shares_diluted,
       b.cash_and_equivalents,
       b.investments_current,
       b.investments_noncurrent,
       b.investments_total,
       b.receivables,
       b.inventory,
       b.tax_assets,
       b.total_current_assets,
       b.property_plant_equipment_net,
       b.goodwill_and_intangibles,
       b.total_noncurrent_assets,
       b.total_assets,
       b.payables,
       b.deferred_revenue,
       b.deposit_liabilities,
       b.debt_current,
       b.debt_noncurrent,
       b.total_debt,
       b.tax_liabilities,
       b.total_current_liabilities,
       b.total_noncurrent_liabilities,
       b.total_liabilities,
       b.accumulated_other_comprehensive_income,
       b.retained_earnings,
       b.total_equity_parent,
       b.shares_outstanding,
       CASE WHEN c.c3 AND c.net_cash_operating_n = 4 THEN c.net_cash_operating_sum END,
       CASE WHEN c.c3 AND c.depreciation_amortization_n = 4 THEN c.depreciation_amortization_sum END,
       CASE WHEN c.c3 AND c.share_based_compensation_n = 4 THEN c.share_based_compensation_sum END,
       CASE WHEN c.c3 AND c.capital_expenditure_n = 4 THEN c.capital_expenditure_sum END,
       CASE WHEN c.c3 AND c.net_cash_business_acquisitions_n = 4 THEN c.net_cash_business_acquisitions_sum END,
       CASE WHEN c.c3 AND c.net_cash_investment_acquisitions_n = 4 THEN c.net_cash_investment_acquisitions_sum END,
       CASE WHEN c.c3 AND c.net_cash_investing_n = 4 THEN c.net_cash_investing_sum END,
       CASE WHEN c.c3 AND c.net_debt_issuance_n = 4 THEN c.net_debt_issuance_sum END,
       CASE WHEN c.c3 AND c.net_equity_issuance_n = 4 THEN c.net_equity_issuance_sum END,
       CASE WHEN c.c3 AND c.dividends_paid_n = 4 THEN c.dividends_paid_sum END,
       CASE WHEN c.c3 AND c.net_cash_financing_n = 4 THEN c.net_cash_financing_sum END,
       CASE WHEN c.c3 AND c.fx_effect_on_cash_n = 4 THEN c.fx_effect_on_cash_sum END,
       CASE WHEN c.c3 AND c.net_change_in_cash_n = 4 THEN c.net_change_in_cash_sum END
  FROM inc_c i
  LEFT JOIN cfs_c c ON c.security_id = i.security_id AND c.fiscal_date = i.fiscal_date
  LEFT JOIN bal b ON b.security_id = i.security_id AND b.fiscal_date = i.fiscal_date
$fn$;

COMMENT ON FUNCTION mart.fn_fundamentals_ttm(date) IS
    'Trailing twelve months per quarter-end in force at as_of: flows summed over '
    'four contiguous quarters or NULL, stocks and share counts at the latest '
    'quarter. known_from is the latest of every constituent (ADR 0018).';

-- ---------------------------------------------------------------------------
-- The current views: screening, not backtests
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_statements_current AS
SELECT * FROM mart.fn_statements_as_of(current_date);

COMMENT ON VIEW mart.v_statements_current IS
    'The latest period per (security_id, period_type) with its statements in force '
    'today. For screening and dashboards, NOT for backtests: it knows today''s '
    'restatements. A backtest calls mart.fn_statements_as_of(as_of).';

CREATE OR REPLACE VIEW mart.v_fundamentals_ttm AS
SELECT * FROM mart.fn_fundamentals_ttm(current_date);

COMMENT ON VIEW mart.v_fundamentals_ttm IS
    'Trailing twelve months per quarter-end, as known today. For screening, NOT '
    'for backtests: a backtest calls mart.fn_fundamentals_ttm(as_of).';

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------
GRANT SELECT ON mart.v_income_statement_versions, mart.v_balance_sheet_versions, mart.v_cash_flow_statement_versions, mart.v_statements_current, mart.v_fundamentals_ttm
    TO fafnir_read, fafnir_app, fafnir_ops;
REVOKE ALL ON FUNCTION mart.fn_statements_as_of(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mart.fn_statements_as_of(date) TO fafnir_read, fafnir_app, fafnir_ops;
REVOKE ALL ON FUNCTION mart.fn_statements_as_of(date, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mart.fn_statements_as_of(date, timestamptz) TO fafnir_read, fafnir_app, fafnir_ops;
REVOKE ALL ON FUNCTION mart.fn_fundamentals_ttm(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mart.fn_fundamentals_ttm(date) TO fafnir_read, fafnir_app, fafnir_ops;

COMMIT;
