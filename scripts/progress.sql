-- progress.sql -- live progress of an in-flight ingestion run.
--
-- ops.ingestion_run.bytes_downloaded / rows_inserted are written once, when the
-- run closes (RunLog.__exit__), so they stay at zero for the hours a backfill is
-- actually working. Every long step does commit per symbol, though, so progress
-- is read from whatever that commit leaves behind -- which differs per endpoint:
--
--   profile                                  -> core.company_profile.loaded_at
--   corporate-actions                        -> ops.load_watermark.updated_at
--   historical-price-eod/non-split-adjusted  -> ops.load_watermark.updated_at
--
-- Corporate actions read the watermark rather than core.corporate_action.loaded_at:
-- since ADR 0007 an upsert that changes nothing leaves loaded_at alone (that is what
-- makes `fafnir adjust --changed` a small set), so a resumed backfill would show a
-- security as not done merely because its history had not moved. The watermark is
-- written once per security either way, which is exactly the "done" being counted.
--
-- `done` is NULL for any other endpoint: that step has no per-symbol trail, so
-- there is nothing to count. NULL means "no signal", never "stuck at zero" --
-- check the `endpoint` column to see which step is actually running.
--
-- Usage (one-shot):
--   psql "${FAFNIR_DSN}" -f scripts/progress.sql
-- Usage (live, refreshing every 30s -- Ctrl-C to stop):
--   watch -n 30 "psql -X -P pager=off \"\${FAFNIR_DSN}\" -f scripts/progress.sql"
-- Or, inside an interactive psql session, paste the query and add: \watch 30
-- (paste it -- `\i progress.sql` then `\watch` fails with "\watch cannot be used
-- with an empty query", because \i does not leave the query in the buffer).
--
-- Returns no rows when nothing is running.
--
-- Caveat: each counter tracks symbols that produced *data*, not symbols
-- attempted -- a price symbol with no clean bars never advances a watermark, and
-- a symbol with no splits or dividends never writes a corporate action. Progress
-- reads slightly low and the ETA slightly long; both stay monotonic.
WITH run AS (
    SELECT ingestion_run_id, endpoint, started_at,
           NULLIF((params->>'symbols')::int, 0) AS total
    FROM ops.ingestion_run
    WHERE status = 'started'
    ORDER BY started_at DESC
    LIMIT 1
), done AS (
    SELECT CASE run.endpoint
        WHEN 'profile' THEN (
            SELECT count(*) FROM core.company_profile p
            WHERE p.loaded_at >= run.started_at)
        WHEN 'corporate-actions' THEN (
            SELECT count(*) FROM ops.load_watermark w
            WHERE w.endpoint = 'corporate-actions'
              AND w.security_id > 0          -- 0 is the market-wide sweep, not a security
              AND w.updated_at >= run.started_at)
        WHEN 'historical-price-eod/non-split-adjusted' THEN (
            SELECT count(*) FROM ops.load_watermark w
            WHERE w.endpoint = run.endpoint AND w.updated_at >= run.started_at)
    END AS n
    FROM run
)
SELECT run.ingestion_run_id                                     AS run,
       run.endpoint                                             AS endpoint,
       done.n                                                   AS done,
       run.total                                                AS total,
       round(100.0 * done.n / run.total, 1)                     AS pct,
       date_trunc('second', now() - run.started_at)             AS elapsed,
       round((done.n / (extract(epoch FROM now() - run.started_at) / 60))::numeric, 1)
                                                                AS per_min,
       date_trunc('second', (now() - run.started_at)
           * ((run.total - done.n)::float / NULLIF(done.n, 0)))  AS eta
FROM run, done;
