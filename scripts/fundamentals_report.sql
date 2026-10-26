-- fundamentals_report.sql -- what the SF1 load wrote, in aggregates (#92).
--
-- The numbers #92 records after `fafnir ingest fundamentals --backfill`, and that a
-- later load can be read against:
--
--   1. the latest fundamentals runs: status, time, rows written and quarantined;
--   2. rows per statement table: current, superseded (vendor corrections), later
--      versions (restatements), securities and periods;
--   3. knowledge_source by the decade of the period, per table (plan §7);
--   4. the share of periods restated, per table and period type;
--   5. the open fundamentals_* flags, by check and severity;
--   6. the balance-sheet identity pass rate, noncontrolling interest aside
--      (plan §6.5; the build prompt's tolerance, max(0.5% of assets, 1,000,000));
--   7. the standing invariants, both 0: nothing known before its period ended,
--      and every period's current versions chained without a gap or an overlap;
--   8. the tables' size on disk.
--
-- Every figure is a count, a share or a size: none is a vendor value, so the
-- output can be posted on a GitHub issue as it is.
--
-- Usage:
--   psql "${FAFNIR_DSN}" -X -f scripts/fundamentals_report.sql
-- Read-only; any role that reads core and ops (fafnir_ops, fafnir_ingest).

\echo '-- 1. the latest fundamentals runs'
SELECT ingestion_run_id                                   AS run,
       endpoint,
       status,
       started_at::timestamp(0)                           AS started,
       date_trunc('second', finished_at - started_at)     AS took,
       rows_inserted,
       rows_quarantined,
       (params ->> 'keys')::int                           AS keys,
       (params ->> 'securities')::int                     AS securities,
       (params ->> 'read')::bigint                        AS rows_read
  FROM ops.ingestion_run
 WHERE source = 'sharadar' AND endpoint IN ('fundamentals', 'fundamentals:nightly')
 ORDER BY ingestion_run_id DESC
 LIMIT 5;

\echo '-- 2. rows per statement table'
SELECT t.tbl                                                        AS statement,
       count(*)                                                     AS rows,
       count(*) FILTER (WHERE superseded_at IS NULL)                AS current_rows,
       count(*) FILTER (WHERE superseded_at IS NOT NULL)            AS superseded,
       count(*) FILTER (WHERE superseded_at IS NULL AND version_no > 1) AS later_versions,
       count(DISTINCT security_id)                                  AS securities,
       count(DISTINCT (security_id, period_type, fiscal_date))      AS periods
  FROM (SELECT 'income_statement' AS tbl, security_id, period_type, fiscal_date,
               version_no, superseded_at FROM core.income_statement
        UNION ALL
        SELECT 'balance_sheet', security_id, period_type, fiscal_date,
               version_no, superseded_at FROM core.balance_sheet
        UNION ALL
        SELECT 'cash_flow_statement', security_id, period_type, fiscal_date,
               version_no, superseded_at FROM core.cash_flow_statement) t
 GROUP BY t.tbl
 ORDER BY t.tbl;

\echo '-- 3. knowledge_source by the decade of the period (current rows)'
SELECT t.tbl                                                AS statement,
       (extract(year FROM fiscal_date)::int / 10 * 10) || 's' AS decade,
       count(*) FILTER (WHERE knowledge_source = 'filed')     AS filed,
       count(*) FILTER (WHERE knowledge_source = 'observed')  AS observed,
       count(*) FILTER (WHERE knowledge_source = 'estimated') AS estimated
  FROM (SELECT 'income_statement' AS tbl, fiscal_date, knowledge_source, superseded_at
          FROM core.income_statement
        UNION ALL
        SELECT 'balance_sheet', fiscal_date, knowledge_source, superseded_at
          FROM core.balance_sheet
        UNION ALL
        SELECT 'cash_flow_statement', fiscal_date, knowledge_source, superseded_at
          FROM core.cash_flow_statement) t
 WHERE superseded_at IS NULL
 GROUP BY 1, 2
 ORDER BY 1, 2;

\echo '-- 4. periods restated, per table and period type'
SELECT t.tbl                                                AS statement,
       period_type,
       count(*)                                             AS periods,
       count(*) FILTER (WHERE versions > 1)                 AS restated,
       round(100.0 * count(*) FILTER (WHERE versions > 1)
             / nullif(count(*), 0), 1)                      AS restated_pct
  FROM (SELECT 'income_statement' AS tbl, period_type, max(version_no) AS versions
          FROM core.income_statement WHERE superseded_at IS NULL
         GROUP BY security_id, period_type, fiscal_date
        UNION ALL
        SELECT 'balance_sheet', period_type, max(version_no)
          FROM core.balance_sheet WHERE superseded_at IS NULL
         GROUP BY security_id, period_type, fiscal_date
        UNION ALL
        SELECT 'cash_flow_statement', period_type, max(version_no)
          FROM core.cash_flow_statement WHERE superseded_at IS NULL
         GROUP BY security_id, period_type, fiscal_date) t
 GROUP BY 1, 2
 ORDER BY 1, 2;

\echo '-- 5. open fundamentals flags'
SELECT check_name, severity, count(*) AS open_flags
  FROM ops.data_quality_flag
 WHERE check_name LIKE 'fundamentals\_%' AND resolved_at IS NULL
 GROUP BY 1, 2
 ORDER BY 1, 2;

\echo '-- 6. balance-sheet identity, versions in force today'
WITH bal AS (
    SELECT b.security_id, b.period_type, b.fiscal_date,
           b.total_assets AS a, b.total_liabilities AS l, b.total_equity_parent AS e
      FROM core.balance_sheet b
     WHERE b.superseded_at IS NULL AND b.known_to IS NULL
), nci AS (
    SELECT DISTINCT i.security_id, i.period_type, i.fiscal_date
      FROM core.income_statement i
     WHERE i.superseded_at IS NULL AND i.known_to IS NULL
       AND coalesce(i.net_income_noncontrolling, 0) <> 0
), judged AS (
    SELECT bal.*,
           (n.security_id IS NOT NULL) AS has_nci,
           CASE WHEN a IS NULL OR l IS NULL OR e IS NULL THEN NULL
                ELSE abs(a - l - e) <= greatest(0.005 * abs(a), 1000000) END AS holds
      FROM bal
      LEFT JOIN nci n USING (security_id, period_type, fiscal_date)
)
SELECT count(*)                                                  AS rows,
       count(*) FILTER (WHERE holds IS NULL)                     AS incomplete,
       count(*) FILTER (WHERE holds)                             AS holds,
       count(*) FILTER (WHERE NOT holds AND has_nci)             AS fails_with_nci,
       count(*) FILTER (WHERE NOT holds AND NOT has_nci)         AS fails_without_nci,
       round(100.0 * count(*) FILTER (WHERE holds OR (NOT holds AND has_nci))
             / nullif(count(*) FILTER (WHERE holds IS NOT NULL), 0), 2)
                                                                 AS pass_pct_nci_aside
  FROM judged;

\echo '-- 7. standing invariants: both columns 0'
SELECT t.tbl                                                AS statement,
       count(*) FILTER (WHERE known_from < fiscal_date)     AS known_before_period_end,
       count(*) FILTER (WHERE superseded_at IS NULL AND (
                    (is_last AND known_to IS NOT NULL)
                 OR (NOT is_last AND known_to IS DISTINCT FROM next_from)))
                                                            AS chain_breaks
  FROM (SELECT tbl, fiscal_date, known_from, known_to, superseded_at,
               lead(known_from) OVER w AS next_from,
               row_number() OVER w = count(*) OVER p AS is_last
          FROM (SELECT 'income_statement' AS tbl, security_id, period_type, fiscal_date,
                       version_no, known_from, known_to, superseded_at
                  FROM core.income_statement
                UNION ALL
                SELECT 'balance_sheet', security_id, period_type, fiscal_date,
                       version_no, known_from, known_to, superseded_at
                  FROM core.balance_sheet
                UNION ALL
                SELECT 'cash_flow_statement', security_id, period_type, fiscal_date,
                       version_no, known_from, known_to, superseded_at
                  FROM core.cash_flow_statement) s
         WHERE superseded_at IS NULL
        WINDOW p AS (PARTITION BY tbl, security_id, period_type, fiscal_date),
               w AS (p ORDER BY version_no)) t
 GROUP BY 1
 ORDER BY 1;

\echo '-- 8. size on disk'
SELECT relname                                         AS statement,
       pg_size_pretty(pg_total_relation_size(c.oid))   AS total,
       pg_size_pretty(pg_relation_size(c.oid))         AS heap,
       pg_size_pretty(pg_indexes_size(c.oid))          AS indexes,
       pg_size_pretty(coalesce(pg_total_relation_size(c.reltoastrelid), 0)) AS toast
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'core'
   AND c.relname IN ('income_statement', 'balance_sheet', 'cash_flow_statement')
 ORDER BY 1;
