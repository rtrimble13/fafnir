-- 0033_corporate_action_mart.up.sql
-- mart.v_corporate_action -- splits and cash dividends, one row per event (#174).
--
-- A fafnir_app role has no USAGE on core (ADR 0008), so a client listing corporate
-- actions had nowhere to read them. The first is portable's `pt ca sync`, which
-- compares the splits and dividends in a date range with its own ledger and, since
-- the owner reaches the warehouse over the tunnel as a fafnir_app member, refuses
-- until this view exists. This is 0020's answer again (ADR 0009): a definer-rights
-- view in mart.
--
-- A MART VIEW IS A GRANT to every mart reader -- every person, every agent. This one
-- exposes split ratios and dividend amounts per ex-date: vendor market data of the
-- same class mart.v_daily_price_raw already exposes, from the same vendors under the
-- same precedence (ADR 0015).
--
-- At event grain, deliberately: mart.v_security_action_summary aggregates and cannot
-- say which dividends went ex between two dates. Lineage (ingestion_run_id,
-- loaded_at) is left out, as in mart.v_daily_price_raw. `source` stays: who supplied a
-- row, or that an operator wrote it (0025), is a fact about the data, not about a
-- load.
--
-- ADR 0009's third rule: this view depends on split_numerator, split_denominator and
-- dividend_amount, so re-typing them means dropping and recreating it too. Accepted:
-- they are the event's own facts, and a reader listing events needs them.
--
-- Do NOT set security_invoker (ADR 0009). SELECT comes from the default privileges
-- 0001 set on mart.

BEGIN;

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
    ca.source
FROM core.corporate_action ca;

COMMENT ON VIEW mart.v_corporate_action IS
    'Split and cash-dividend events at (security_id, action_type, ex_date) grain. '
    'Passthrough of core.corporate_action without load lineage.';

COMMIT;
