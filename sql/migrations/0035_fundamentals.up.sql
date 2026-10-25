-- 0035_fundamentals.up.sql
-- Fundamentals: the three statement tables and their line-item taxonomy (#87;
-- ADR 0018, ADR 0019; plan doc/plans/fundamentals-sf1.md §4).
--
-- core.income_statement, core.balance_sheet and core.cash_flow_statement hold SF1's
-- statement indicators, one row per version of a period, on two timelines:
--
--   knowledge  known_from / known_to: when the market could know this version. A
--              first-reported row (ARQ, ARY) is version 1, known from its SEC filing
--              ('filed'). A restated row (MRQ, MRY) that differs is a new version,
--              known from the day fafnir loads it ('observed'): SF1 dates no
--              restatement. A period with no first-reported row is known from the
--              issuer's next filing ('estimated'). Half-open: a restatement sets the
--              previous version's known_to to its own known_from, so a period's
--              versions chain with no gap or overlap. Successive periods are not
--              chained.
--   load       loaded_at / superseded_at: when fafnir held this row. A vendor
--              correction (a first-reported row that changed in SF1: the filing did
--              not) supersedes the row and inserts the corrected one with the same
--              version and knowledge interval.
--
-- Two updates in place, and only two: known_to (NULL to a date, once, with the next
-- version's insert) and superseded_at (NULL to a time, once). A value is never
-- updated. valid_from / valid_to are not used: elsewhere in the warehouse they are
-- inclusive dates, and these intervals are half-open.
--
-- Share terms (ADR 0018 §4). A version is stored in the share terms of its
-- known_from: a first-reported row's share counts are SF1's divided by Sharadar's
-- splits after the filing, a restated row's are as given. So a later split changes
-- nothing stored. No per-share column: the mart derives EPS (ADR 0019). SF1's
-- metrics, per-share values and *usd conversions go to extra, which mart never
-- reads.
--
-- The warehouse has no exclusion constraints, so "no gap, no overlap" between a
-- period's versions is a standing test and a DQ check, not a constraint. The
-- unique indexes below allow one current row per version and one version in force
-- per period.
--
-- ref.statement_line_item is the taxonomy: which SF1 indicator fills which column,
-- and how TTM reads it (item_class decides what may be summed). Its rows are
-- inserted here, not in sql/seeds, so an upgraded host has them after `fafnir db
-- migrate` alone, as 0019 and 0027 did for exchanges. A test holds them equal to
-- fafnir.ingest.fundamentals_map.
--
-- Deleting a security that holds statements is refused by the foreign key, never
-- cascaded: `security merge` and `dedupe` learn to move statements with the loader
-- (#90). Until then nothing writes these tables.
--
-- Privileges. Default privileges (0001, 0021) already give fafnir_read and
-- fafnir_ops SELECT on new core and ref tables, but only for objects the role that
-- set them creates. The grants below repeat them explicitly, so a different
-- migrator cannot leave a table unreadable. fafnir_app reads ref, not core: mart
-- views will expose the statements (#95). No function is created here; default
-- privileges do not cover functions, and #95's say how EXECUTE is granted.
--
-- Empty on arrival: nothing here changes a row that exists.

BEGIN;

-- ---------------------------------------------------------------------------
-- core.income_statement
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.income_statement (
    security_id        BIGINT      NOT NULL REFERENCES core.security (security_id),
    vendor_key         TEXT        NOT NULL,
    period_type        TEXT        NOT NULL,
    fiscal_date        DATE        NOT NULL,
    calendar_date      DATE        NOT NULL,
    fiscal_year        INTEGER     NOT NULL,
    fiscal_period      TEXT        NOT NULL,
    period_start_date  DATE,
    version_no         INTEGER     NOT NULL,
    known_from         DATE        NOT NULL,
    known_to           DATE,
    knowledge_source   TEXT        NOT NULL,
    filing_date        DATE,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at      TIMESTAMPTZ,
    reported_currency  TEXT,
    revenue                            NUMERIC(24,2),
    cost_of_revenue                    NUMERIC(24,2),
    gross_profit                       NUMERIC(24,2),
    research_and_development           NUMERIC(24,2),
    selling_general_and_administrative NUMERIC(24,2),
    operating_expenses                 NUMERIC(24,2),
    operating_income                   NUMERIC(24,2),
    interest_expense                   NUMERIC(24,2),
    ebit                               NUMERIC(24,2),
    income_before_tax                  NUMERIC(24,2),
    income_tax_expense                 NUMERIC(24,2),
    consolidated_income                NUMERIC(24,2),
    net_income_noncontrolling          NUMERIC(24,2),
    net_income_discontinued            NUMERIC(24,2),
    net_income                         NUMERIC(24,2),
    preferred_dividends                NUMERIC(24,2),
    net_income_common                  NUMERIC(24,2),
    weighted_average_shares            NUMERIC(24,4),
    weighted_average_shares_diluted    NUMERIC(24,4),
    content_hash       TEXT        NOT NULL,
    extra              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    source             TEXT        NOT NULL DEFAULT 'sharadar',
    ingestion_run_id   BIGINT      REFERENCES ops.ingestion_run (ingestion_run_id),
    PRIMARY KEY (security_id, period_type, fiscal_date, version_no, loaded_at),
    CONSTRAINT ck_inc_period_type CHECK (period_type IN ('annual', 'quarter')),
    CONSTRAINT ck_inc_fiscal_period
        CHECK (fiscal_period IN ('FY', 'Q1', 'Q2', 'Q3', 'Q4')),
    CONSTRAINT ck_inc_period_label CHECK ((period_type = 'annual') = (fiscal_period = 'FY')),
    CONSTRAINT ck_inc_period_start
        CHECK (period_start_date IS NULL OR period_start_date < fiscal_date),
    CONSTRAINT ck_inc_version CHECK (version_no >= 1),
    -- The look-ahead tripwire: nothing is known before its period ended.
    CONSTRAINT ck_inc_known_after_period CHECK (known_from >= fiscal_date),
    CONSTRAINT ck_inc_known_range CHECK (known_to IS NULL OR known_to > known_from),
    CONSTRAINT ck_inc_knowledge_source
        CHECK (knowledge_source IN ('filed', 'observed', 'estimated')),
    -- Only a filed version has a filing date, and it is known from it.
    CONSTRAINT ck_inc_filed CHECK (
        (knowledge_source = 'filed') = (filing_date IS NOT NULL)
        AND (knowledge_source <> 'filed' OR known_from = filing_date)
    ),
    -- A later version is a restatement, which SF1 never dates: fafnir observed it.
    CONSTRAINT ck_inc_restatement_observed
        CHECK (version_no = 1 OR knowledge_source = 'observed'),
    CONSTRAINT ck_inc_superseded CHECK (superseded_at IS NULL OR superseded_at >= loaded_at),
    CONSTRAINT ck_inc_content_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- No other vendor writes statements (ADR 0018).
    CONSTRAINT ck_inc_source CHECK (source = 'sharadar')
);

COMMENT ON TABLE core.income_statement IS
    'Income statements from Sharadar SF1 (ADR 0018). Grain: (security_id, period_type, '
    'fiscal_date, version_no, loaded_at): '
    'one row per version of a period, per load. In force at D: superseded_at IS NULL '
    'AND known_from <= D AND (known_to IS NULL OR D < known_to). Line items: '
    'ref.statement_line_item. Cadence: the SF1 backfill, then the nightly delta.';
COMMENT ON COLUMN core.income_statement.fiscal_date IS
    'SF1 reportperiod: the end of the period the figures describe.';
COMMENT ON COLUMN core.income_statement.calendar_date IS
    'SF1 calendardate: the vendor''s normalized period, for comparing companies.';
COMMENT ON COLUMN core.income_statement.version_no IS
    '1 = as first reported (or the first version fafnir has); each restatement adds one.';
COMMENT ON COLUMN core.income_statement.known_from IS
    'First day the market could know this version: the filing date (filed), the day '
    'fafnir first loaded a restatement (observed), or the issuer''s next filing '
    '(estimated). Its share counts are in the share terms of this date.';
COMMENT ON COLUMN core.income_statement.known_to IS
    'The next version''s known_from; NULL while this version is in force. Half-open.';
COMMENT ON COLUMN core.income_statement.superseded_at IS
    'When a vendor correction replaced this row; NULL for the current row of a version.';
COMMENT ON COLUMN core.income_statement.extra IS
    'SF1 fields without a typed column; never read by mart (ADR 0019).';

-- One current row per version (load timeline).
CREATE UNIQUE INDEX IF NOT EXISTS ux_income_statement_current
    ON core.income_statement (security_id, period_type, fiscal_date, version_no)
    WHERE superseded_at IS NULL;
-- One version in force per period (knowledge timeline).
CREATE UNIQUE INDEX IF NOT EXISTS ux_income_statement_in_force
    ON core.income_statement (security_id, period_type, fiscal_date)
    WHERE known_to IS NULL AND superseded_at IS NULL;
-- Point-in-time lookup: the latest period known by a date.
CREATE INDEX IF NOT EXISTS ix_income_statement_pit
    ON core.income_statement (security_id, period_type, known_from DESC, fiscal_date DESC)
    WHERE superseded_at IS NULL;
-- Panel builds sweep the universe by knowledge date.
CREATE INDEX IF NOT EXISTS ix_income_statement_known
    ON core.income_statement (known_from) WHERE superseded_at IS NULL;
-- Restatement forensics.
CREATE INDEX IF NOT EXISTS ix_income_statement_restated
    ON core.income_statement (security_id, fiscal_date) WHERE version_no > 1;

-- ---------------------------------------------------------------------------
-- core.balance_sheet
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.balance_sheet (
    security_id        BIGINT      NOT NULL REFERENCES core.security (security_id),
    vendor_key         TEXT        NOT NULL,
    period_type        TEXT        NOT NULL,
    fiscal_date        DATE        NOT NULL,
    calendar_date      DATE        NOT NULL,
    fiscal_year        INTEGER     NOT NULL,
    fiscal_period      TEXT        NOT NULL,
    period_start_date  DATE,
    version_no         INTEGER     NOT NULL,
    known_from         DATE        NOT NULL,
    known_to           DATE,
    knowledge_source   TEXT        NOT NULL,
    filing_date        DATE,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at      TIMESTAMPTZ,
    reported_currency  TEXT,
    cash_and_equivalents                   NUMERIC(24,2),
    investments_current                    NUMERIC(24,2),
    investments_noncurrent                 NUMERIC(24,2),
    investments_total                      NUMERIC(24,2),
    receivables                            NUMERIC(24,2),
    inventory                              NUMERIC(24,2),
    tax_assets                             NUMERIC(24,2),
    total_current_assets                   NUMERIC(24,2),
    property_plant_equipment_net           NUMERIC(24,2),
    goodwill_and_intangibles               NUMERIC(24,2),
    total_noncurrent_assets                NUMERIC(24,2),
    total_assets                           NUMERIC(24,2),
    payables                               NUMERIC(24,2),
    deferred_revenue                       NUMERIC(24,2),
    deposit_liabilities                    NUMERIC(24,2),
    debt_current                           NUMERIC(24,2),
    debt_noncurrent                        NUMERIC(24,2),
    total_debt                             NUMERIC(24,2),
    tax_liabilities                        NUMERIC(24,2),
    total_current_liabilities              NUMERIC(24,2),
    total_noncurrent_liabilities           NUMERIC(24,2),
    total_liabilities                      NUMERIC(24,2),
    accumulated_other_comprehensive_income NUMERIC(24,2),
    retained_earnings                      NUMERIC(24,2),
    total_equity_parent                    NUMERIC(24,2),
    shares_outstanding                     NUMERIC(24,4),
    content_hash       TEXT        NOT NULL,
    extra              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    source             TEXT        NOT NULL DEFAULT 'sharadar',
    ingestion_run_id   BIGINT      REFERENCES ops.ingestion_run (ingestion_run_id),
    PRIMARY KEY (security_id, period_type, fiscal_date, version_no, loaded_at),
    CONSTRAINT ck_bal_period_type CHECK (period_type IN ('annual', 'quarter')),
    CONSTRAINT ck_bal_fiscal_period
        CHECK (fiscal_period IN ('FY', 'Q1', 'Q2', 'Q3', 'Q4')),
    CONSTRAINT ck_bal_period_label CHECK ((period_type = 'annual') = (fiscal_period = 'FY')),
    CONSTRAINT ck_bal_period_start
        CHECK (period_start_date IS NULL OR period_start_date < fiscal_date),
    CONSTRAINT ck_bal_version CHECK (version_no >= 1),
    -- The look-ahead tripwire: nothing is known before its period ended.
    CONSTRAINT ck_bal_known_after_period CHECK (known_from >= fiscal_date),
    CONSTRAINT ck_bal_known_range CHECK (known_to IS NULL OR known_to > known_from),
    CONSTRAINT ck_bal_knowledge_source
        CHECK (knowledge_source IN ('filed', 'observed', 'estimated')),
    -- Only a filed version has a filing date, and it is known from it.
    CONSTRAINT ck_bal_filed CHECK (
        (knowledge_source = 'filed') = (filing_date IS NOT NULL)
        AND (knowledge_source <> 'filed' OR known_from = filing_date)
    ),
    -- A later version is a restatement, which SF1 never dates: fafnir observed it.
    CONSTRAINT ck_bal_restatement_observed
        CHECK (version_no = 1 OR knowledge_source = 'observed'),
    CONSTRAINT ck_bal_superseded CHECK (superseded_at IS NULL OR superseded_at >= loaded_at),
    CONSTRAINT ck_bal_content_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- No other vendor writes statements (ADR 0018).
    CONSTRAINT ck_bal_source CHECK (source = 'sharadar')
);

COMMENT ON TABLE core.balance_sheet IS
    'Balance sheets from Sharadar SF1 (ADR 0018). Grain: (security_id, period_type, '
    'fiscal_date, version_no, loaded_at): '
    'one row per version of a period, per load. In force at D: superseded_at IS NULL '
    'AND known_from <= D AND (known_to IS NULL OR D < known_to). Line items: '
    'ref.statement_line_item. Cadence: the SF1 backfill, then the nightly delta.';
COMMENT ON COLUMN core.balance_sheet.fiscal_date IS
    'SF1 reportperiod: the end of the period the figures describe.';
COMMENT ON COLUMN core.balance_sheet.calendar_date IS
    'SF1 calendardate: the vendor''s normalized period, for comparing companies.';
COMMENT ON COLUMN core.balance_sheet.version_no IS
    '1 = as first reported (or the first version fafnir has); each restatement adds one.';
COMMENT ON COLUMN core.balance_sheet.known_from IS
    'First day the market could know this version: the filing date (filed), the day '
    'fafnir first loaded a restatement (observed), or the issuer''s next filing '
    '(estimated). Its share counts are in the share terms of this date.';
COMMENT ON COLUMN core.balance_sheet.known_to IS
    'The next version''s known_from; NULL while this version is in force. Half-open.';
COMMENT ON COLUMN core.balance_sheet.superseded_at IS
    'When a vendor correction replaced this row; NULL for the current row of a version.';
COMMENT ON COLUMN core.balance_sheet.extra IS
    'SF1 fields without a typed column; never read by mart (ADR 0019).';

-- One current row per version (load timeline).
CREATE UNIQUE INDEX IF NOT EXISTS ux_balance_sheet_current
    ON core.balance_sheet (security_id, period_type, fiscal_date, version_no)
    WHERE superseded_at IS NULL;
-- One version in force per period (knowledge timeline).
CREATE UNIQUE INDEX IF NOT EXISTS ux_balance_sheet_in_force
    ON core.balance_sheet (security_id, period_type, fiscal_date)
    WHERE known_to IS NULL AND superseded_at IS NULL;
-- Point-in-time lookup: the latest period known by a date.
CREATE INDEX IF NOT EXISTS ix_balance_sheet_pit
    ON core.balance_sheet (security_id, period_type, known_from DESC, fiscal_date DESC)
    WHERE superseded_at IS NULL;
-- Panel builds sweep the universe by knowledge date.
CREATE INDEX IF NOT EXISTS ix_balance_sheet_known
    ON core.balance_sheet (known_from) WHERE superseded_at IS NULL;
-- Restatement forensics.
CREATE INDEX IF NOT EXISTS ix_balance_sheet_restated
    ON core.balance_sheet (security_id, fiscal_date) WHERE version_no > 1;

-- ---------------------------------------------------------------------------
-- core.cash_flow_statement
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.cash_flow_statement (
    security_id        BIGINT      NOT NULL REFERENCES core.security (security_id),
    vendor_key         TEXT        NOT NULL,
    period_type        TEXT        NOT NULL,
    fiscal_date        DATE        NOT NULL,
    calendar_date      DATE        NOT NULL,
    fiscal_year        INTEGER     NOT NULL,
    fiscal_period      TEXT        NOT NULL,
    period_start_date  DATE,
    version_no         INTEGER     NOT NULL,
    known_from         DATE        NOT NULL,
    known_to           DATE,
    knowledge_source   TEXT        NOT NULL,
    filing_date        DATE,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at      TIMESTAMPTZ,
    reported_currency  TEXT,
    net_cash_operating               NUMERIC(24,2),
    depreciation_amortization        NUMERIC(24,2),
    share_based_compensation         NUMERIC(24,2),
    capital_expenditure              NUMERIC(24,2),
    net_cash_business_acquisitions   NUMERIC(24,2),
    net_cash_investment_acquisitions NUMERIC(24,2),
    net_cash_investing               NUMERIC(24,2),
    net_debt_issuance                NUMERIC(24,2),
    net_equity_issuance              NUMERIC(24,2),
    dividends_paid                   NUMERIC(24,2),
    net_cash_financing               NUMERIC(24,2),
    fx_effect_on_cash                NUMERIC(24,2),
    net_change_in_cash               NUMERIC(24,2),
    content_hash       TEXT        NOT NULL,
    extra              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    source             TEXT        NOT NULL DEFAULT 'sharadar',
    ingestion_run_id   BIGINT      REFERENCES ops.ingestion_run (ingestion_run_id),
    PRIMARY KEY (security_id, period_type, fiscal_date, version_no, loaded_at),
    CONSTRAINT ck_cfs_period_type CHECK (period_type IN ('annual', 'quarter')),
    CONSTRAINT ck_cfs_fiscal_period
        CHECK (fiscal_period IN ('FY', 'Q1', 'Q2', 'Q3', 'Q4')),
    CONSTRAINT ck_cfs_period_label CHECK ((period_type = 'annual') = (fiscal_period = 'FY')),
    CONSTRAINT ck_cfs_period_start
        CHECK (period_start_date IS NULL OR period_start_date < fiscal_date),
    CONSTRAINT ck_cfs_version CHECK (version_no >= 1),
    -- The look-ahead tripwire: nothing is known before its period ended.
    CONSTRAINT ck_cfs_known_after_period CHECK (known_from >= fiscal_date),
    CONSTRAINT ck_cfs_known_range CHECK (known_to IS NULL OR known_to > known_from),
    CONSTRAINT ck_cfs_knowledge_source
        CHECK (knowledge_source IN ('filed', 'observed', 'estimated')),
    -- Only a filed version has a filing date, and it is known from it.
    CONSTRAINT ck_cfs_filed CHECK (
        (knowledge_source = 'filed') = (filing_date IS NOT NULL)
        AND (knowledge_source <> 'filed' OR known_from = filing_date)
    ),
    -- A later version is a restatement, which SF1 never dates: fafnir observed it.
    CONSTRAINT ck_cfs_restatement_observed
        CHECK (version_no = 1 OR knowledge_source = 'observed'),
    CONSTRAINT ck_cfs_superseded CHECK (superseded_at IS NULL OR superseded_at >= loaded_at),
    CONSTRAINT ck_cfs_content_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- No other vendor writes statements (ADR 0018).
    CONSTRAINT ck_cfs_source CHECK (source = 'sharadar')
);

COMMENT ON TABLE core.cash_flow_statement IS
    'Cash-flow statements from Sharadar SF1 (ADR 0018). Grain: (security_id, period_type, '
    'fiscal_date, version_no, loaded_at): '
    'one row per version of a period, per load. In force at D: superseded_at IS NULL '
    'AND known_from <= D AND (known_to IS NULL OR D < known_to). Line items: '
    'ref.statement_line_item. Cadence: the SF1 backfill, then the nightly delta.';
COMMENT ON COLUMN core.cash_flow_statement.fiscal_date IS
    'SF1 reportperiod: the end of the period the figures describe.';
COMMENT ON COLUMN core.cash_flow_statement.calendar_date IS
    'SF1 calendardate: the vendor''s normalized period, for comparing companies.';
COMMENT ON COLUMN core.cash_flow_statement.version_no IS
    '1 = as first reported (or the first version fafnir has); each restatement adds one.';
COMMENT ON COLUMN core.cash_flow_statement.known_from IS
    'First day the market could know this version: the filing date (filed), the day '
    'fafnir first loaded a restatement (observed), or the issuer''s next filing '
    '(estimated). Its share counts are in the share terms of this date.';
COMMENT ON COLUMN core.cash_flow_statement.known_to IS
    'The next version''s known_from; NULL while this version is in force. Half-open.';
COMMENT ON COLUMN core.cash_flow_statement.superseded_at IS
    'When a vendor correction replaced this row; NULL for the current row of a version.';
COMMENT ON COLUMN core.cash_flow_statement.extra IS
    'SF1 fields without a typed column; never read by mart (ADR 0019).';

-- One current row per version (load timeline).
CREATE UNIQUE INDEX IF NOT EXISTS ux_cash_flow_statement_current
    ON core.cash_flow_statement (security_id, period_type, fiscal_date, version_no)
    WHERE superseded_at IS NULL;
-- One version in force per period (knowledge timeline).
CREATE UNIQUE INDEX IF NOT EXISTS ux_cash_flow_statement_in_force
    ON core.cash_flow_statement (security_id, period_type, fiscal_date)
    WHERE known_to IS NULL AND superseded_at IS NULL;
-- Point-in-time lookup: the latest period known by a date.
CREATE INDEX IF NOT EXISTS ix_cash_flow_statement_pit
    ON core.cash_flow_statement (security_id, period_type, known_from DESC, fiscal_date DESC)
    WHERE superseded_at IS NULL;
-- Panel builds sweep the universe by knowledge date.
CREATE INDEX IF NOT EXISTS ix_cash_flow_statement_known
    ON core.cash_flow_statement (known_from) WHERE superseded_at IS NULL;
-- Restatement forensics.
CREATE INDEX IF NOT EXISTS ix_cash_flow_statement_restated
    ON core.cash_flow_statement (security_id, fiscal_date) WHERE version_no > 1;

-- ---------------------------------------------------------------------------
-- ref.statement_line_item: the taxonomy
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.statement_line_item (
    statement        TEXT    NOT NULL,
    column_name      TEXT    NOT NULL,
    vendor_field     TEXT    NOT NULL,
    fmp_field        TEXT,
    item_class       TEXT    NOT NULL,
    sign_convention  TEXT    NOT NULL,
    is_subtotal      BOOLEAN NOT NULL DEFAULT FALSE,
    description      TEXT    NOT NULL,
    PRIMARY KEY (statement, column_name),
    CONSTRAINT uq_sli_vendor_field UNIQUE (statement, vendor_field),
    CONSTRAINT ck_sli_statement CHECK (statement IN ('income', 'balance_sheet', 'cash_flow')),
    CONSTRAINT ck_sli_item_class
        CHECK (item_class IN ('flow', 'stock', 'per_share', 'share_count', 'memo')),
    CONSTRAINT ck_sli_sign
        CHECK (sign_convention IN ('positive_is_inflow', 'positive_is_outflow', 'signed'))
);

COMMENT ON TABLE ref.statement_line_item IS
    'The fundamentals taxonomy: one row per typed line item of the statement tables. '
    'vendor_field is the SF1 indicator; fmp_field the FMP spelling of its build-prompt '
    'counterpart, where one exists. item_class is load-bearing: TTM sums a flow and '
    'takes the latest stock or share_count. Grain: (statement, column_name).';
COMMENT ON COLUMN ref.statement_line_item.sign_convention IS
    'positive_is_inflow: a positive value adds cash or revenue (cash-flow lines are '
    'signed as cash moves, so capex is negative); positive_is_outflow: a positive '
    'value is a cost; signed: either sign is meaningful (subtotals, balances).';

INSERT INTO ref.statement_line_item
    (statement, column_name, vendor_field, fmp_field, item_class, sign_convention,
     is_subtotal, description)
VALUES
    ('income', 'revenue', 'revenue', 'revenue',
     'flow', 'positive_is_inflow', FALSE,
     'Revenue for the period'),
    ('income', 'cost_of_revenue', 'cor', 'costOfRevenue',
     'flow', 'positive_is_outflow', FALSE,
     'Cost of the goods and services sold'),
    ('income', 'gross_profit', 'gp', 'grossProfit',
     'flow', 'signed', TRUE,
     'Revenue less cost of revenue'),
    ('income', 'research_and_development', 'rnd', 'researchAndDevelopmentExpenses',
     'flow', 'positive_is_outflow', FALSE,
     'Research and development expense; 0 when the filing does not carry it'),
    ('income', 'selling_general_and_administrative', 'sgna', 'sellingGeneralAndAdministrativeExpenses',
     'flow', 'positive_is_outflow', FALSE,
     'Selling, general and administrative expense'),
    ('income', 'operating_expenses', 'opex', 'operatingExpenses',
     'flow', 'positive_is_outflow', TRUE,
     'Operating expenses: SG&A, R&D and other operating costs'),
    ('income', 'operating_income', 'opinc', 'operatingIncome',
     'flow', 'signed', TRUE,
     'Operating income'),
    ('income', 'interest_expense', 'intexp', 'interestExpense',
     'flow', 'positive_is_outflow', FALSE,
     'Interest expense; 0 when the filing does not carry it'),
    ('income', 'ebit', 'ebit', 'ebit',
     'flow', 'signed', TRUE,
     'Earnings before interest and tax (net income + tax + interest)'),
    ('income', 'income_before_tax', 'ebt', 'incomeBeforeTax',
     'flow', 'signed', TRUE,
     'Earnings before tax (net income + tax)'),
    ('income', 'income_tax_expense', 'taxexp', 'incomeTaxExpense',
     'flow', 'positive_is_outflow', FALSE,
     'Income tax expense; a benefit is negative'),
    ('income', 'consolidated_income', 'consolinc', NULL,
     'flow', 'signed', TRUE,
     'Income after tax of the consolidated entity, before noncontrolling interest'),
    ('income', 'net_income_noncontrolling', 'netincnci', NULL,
     'flow', 'signed', FALSE,
     'Net income attributable to noncontrolling interests'),
    ('income', 'net_income_discontinued', 'netincdis', 'netIncomeFromDiscontinuedOperations',
     'flow', 'signed', FALSE,
     'Net income (loss) from discontinued operations'),
    ('income', 'net_income', 'netinc', 'netIncome',
     'flow', 'signed', TRUE,
     'Net income attributable to the parent'),
    ('income', 'preferred_dividends', 'prefdivis', NULL,
     'flow', 'positive_is_outflow', FALSE,
     'Preferred dividends charged against net income'),
    ('income', 'net_income_common', 'netinccmn', NULL,
     'flow', 'signed', TRUE,
     'Net income available to common shareholders; the numerator of EPS'),
    ('income', 'weighted_average_shares', 'shareswa', 'weightedAverageShsOut',
     'share_count', 'signed', FALSE,
     'Weighted average basic shares of the period, in the version''s share terms'),
    ('income', 'weighted_average_shares_diluted', 'shareswadil', 'weightedAverageShsOutDil',
     'share_count', 'signed', FALSE,
     'Weighted average diluted shares of the period, in the version''s share terms'),
    ('balance_sheet', 'cash_and_equivalents', 'cashneq', 'cashAndCashEquivalents',
     'stock', 'signed', FALSE,
     'Cash and cash equivalents'),
    ('balance_sheet', 'investments_current', 'investmentsc', 'shortTermInvestments',
     'stock', 'signed', FALSE,
     'Current investments'),
    ('balance_sheet', 'investments_noncurrent', 'investmentsnc', 'longTermInvestments',
     'stock', 'signed', FALSE,
     'Non-current investments'),
    ('balance_sheet', 'investments_total', 'investments', 'totalInvestments',
     'stock', 'signed', TRUE,
     'Current and non-current investments'),
    ('balance_sheet', 'receivables', 'receivables', 'netReceivables',
     'stock', 'signed', FALSE,
     'Trade and non-trade receivables'),
    ('balance_sheet', 'inventory', 'inventory', 'inventory',
     'stock', 'signed', FALSE,
     'Inventory'),
    ('balance_sheet', 'tax_assets', 'taxassets', 'taxAssets',
     'stock', 'signed', FALSE,
     'Tax assets'),
    ('balance_sheet', 'total_current_assets', 'assetsc', 'totalCurrentAssets',
     'stock', 'signed', TRUE,
     'Current assets; NULL for a balance sheet that is not classified'),
    ('balance_sheet', 'property_plant_equipment_net', 'ppnenet', 'propertyPlantEquipmentNet',
     'stock', 'signed', FALSE,
     'Property, plant and equipment, net'),
    ('balance_sheet', 'goodwill_and_intangibles', 'intangibles', 'goodwillAndIntangibleAssets',
     'stock', 'signed', FALSE,
     'Goodwill and intangible assets'),
    ('balance_sheet', 'total_noncurrent_assets', 'assetsnc', 'totalNonCurrentAssets',
     'stock', 'signed', TRUE,
     'Non-current assets; NULL for a balance sheet that is not classified'),
    ('balance_sheet', 'total_assets', 'assets', 'totalAssets',
     'stock', 'signed', TRUE,
     'Total assets'),
    ('balance_sheet', 'payables', 'payables', 'totalPayables',
     'stock', 'signed', FALSE,
     'Trade and non-trade payables'),
    ('balance_sheet', 'deferred_revenue', 'deferredrev', 'deferredRevenue',
     'stock', 'signed', FALSE,
     'Deferred revenue'),
    ('balance_sheet', 'deposit_liabilities', 'deposits', NULL,
     'stock', 'signed', FALSE,
     'Deposit liabilities (banks)'),
    ('balance_sheet', 'debt_current', 'debtc', 'shortTermDebt',
     'stock', 'signed', FALSE,
     'Current debt; NULL for a balance sheet that is not classified'),
    ('balance_sheet', 'debt_noncurrent', 'debtnc', 'longTermDebt',
     'stock', 'signed', FALSE,
     'Non-current debt; NULL for a balance sheet that is not classified'),
    ('balance_sheet', 'total_debt', 'debt', 'totalDebt',
     'stock', 'signed', TRUE,
     'Current and non-current debt'),
    ('balance_sheet', 'tax_liabilities', 'taxliabilities', NULL,
     'stock', 'signed', FALSE,
     'Tax liabilities'),
    ('balance_sheet', 'total_current_liabilities', 'liabilitiesc', 'totalCurrentLiabilities',
     'stock', 'signed', TRUE,
     'Current liabilities; NULL for a balance sheet that is not classified'),
    ('balance_sheet', 'total_noncurrent_liabilities', 'liabilitiesnc', 'totalNonCurrentLiabilities',
     'stock', 'signed', TRUE,
     'Non-current liabilities; NULL for a balance sheet that is not classified'),
    ('balance_sheet', 'total_liabilities', 'liabilities', 'totalLiabilities',
     'stock', 'signed', TRUE,
     'Total liabilities'),
    ('balance_sheet', 'accumulated_other_comprehensive_income', 'accoci', 'accumulatedOtherComprehensiveIncomeLoss',
     'stock', 'signed', FALSE,
     'Accumulated other comprehensive income (loss)'),
    ('balance_sheet', 'retained_earnings', 'retearn', 'retainedEarnings',
     'stock', 'signed', FALSE,
     'Retained earnings (deficit)'),
    ('balance_sheet', 'total_equity_parent', 'equity', 'totalStockholdersEquity',
     'stock', 'signed', TRUE,
     'Shareholders'' equity attributable to the parent; no noncontrolling interest'),
    ('balance_sheet', 'shares_outstanding', 'sharesbas', NULL,
     'share_count', 'signed', FALSE,
     'Shares outstanding on the filing''s cover, in the version''s share terms; first-reported rows only'),
    ('cash_flow', 'net_cash_operating', 'ncfo', 'netCashProvidedByOperatingActivities',
     'flow', 'positive_is_inflow', TRUE,
     'Net cash from operating activities'),
    ('cash_flow', 'depreciation_amortization', 'depamor', 'depreciationAndAmortization',
     'flow', 'positive_is_inflow', FALSE,
     'Depreciation, amortization and accretion, added back'),
    ('cash_flow', 'share_based_compensation', 'sbcomp', 'stockBasedCompensation',
     'flow', 'positive_is_inflow', FALSE,
     'Share-based compensation, added back'),
    ('cash_flow', 'capital_expenditure', 'capex', 'capitalExpenditure',
     'flow', 'positive_is_inflow', FALSE,
     'Capital expenditure; an outflow is negative'),
    ('cash_flow', 'net_cash_business_acquisitions', 'ncfbus', 'acquisitionsNet',
     'flow', 'positive_is_inflow', FALSE,
     'Net cash from business acquisitions and disposals'),
    ('cash_flow', 'net_cash_investment_acquisitions', 'ncfinv', NULL,
     'flow', 'positive_is_inflow', FALSE,
     'Net cash from purchases and sales of investments'),
    ('cash_flow', 'net_cash_investing', 'ncfi', 'netCashProvidedByInvestingActivities',
     'flow', 'positive_is_inflow', TRUE,
     'Net cash from investing activities'),
    ('cash_flow', 'net_debt_issuance', 'ncfdebt', 'netDebtIssuance',
     'flow', 'positive_is_inflow', FALSE,
     'Net cash from issuing and repaying debt'),
    ('cash_flow', 'net_equity_issuance', 'ncfcommon', 'netCommonStockIssuance',
     'flow', 'positive_is_inflow', FALSE,
     'Net cash from issuing and repurchasing equity; a buyback is negative'),
    ('cash_flow', 'dividends_paid', 'ncfdiv', 'netDividendsPaid',
     'flow', 'positive_is_inflow', FALSE,
     'Dividends and other cash distributions paid; an outflow is negative'),
    ('cash_flow', 'net_cash_financing', 'ncff', 'netCashProvidedByFinancingActivities',
     'flow', 'positive_is_inflow', TRUE,
     'Net cash from financing activities'),
    ('cash_flow', 'fx_effect_on_cash', 'ncfx', 'effectOfForexChangesOnCash',
     'flow', 'positive_is_inflow', FALSE,
     'Effect of exchange-rate changes on cash'),
    ('cash_flow', 'net_change_in_cash', 'ncf', 'netChangeInCash',
     'flow', 'positive_is_inflow', TRUE,
     'Net change in cash and equivalents')
ON CONFLICT (statement, column_name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Privileges, explicitly (see the header)
-- ---------------------------------------------------------------------------
GRANT SELECT ON core.income_statement, core.balance_sheet, core.cash_flow_statement
    TO fafnir_read, fafnir_ops;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON core.income_statement, core.balance_sheet, core.cash_flow_statement
    TO fafnir_ingest;
GRANT SELECT ON ref.statement_line_item TO fafnir_read, fafnir_app, fafnir_ops;
GRANT SELECT, INSERT, UPDATE, DELETE ON ref.statement_line_item TO fafnir_ingest;

COMMIT;
