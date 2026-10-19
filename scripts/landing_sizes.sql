-- landing_sizes.sql -- what the warehouse and the Sharadar files take, now.
--
-- Plan v2 keeps Sharadar's files on disk under [sharadar] bulk_dir, recorded in
-- landing.sharadar_file, and loads core straight from them (#50). So disk use has
-- two parts. This report shows both, next to the projection in
-- doc/backfill.md › "Disk budget with Sharadar":
--
--   1. the database's own relations: landing, core.daily_price by source, and the
--      rest of core;
--   2. the files the ledger says are on disk, by table and status. The ledger
--      records each file's size when it is fetched; `df` on bulk_dir is the
--      check that they are still there.
--
-- Usage:
--   psql "${FAFNIR_DSN}" -X -f scripts/landing_sizes.sql
-- Read-only; any role that can read landing and core (fafnir_ops, fafnir_ingest).

\echo '-- database relations (core.daily_price: its partitions, together)'
SELECT n.nspname || '.' || c.relname                  AS relation,
       pg_size_pretty(pg_total_relation_size(c.oid))  AS total,
       greatest(c.reltuples, 0)::bigint               AS est_rows
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'r'
   AND n.nspname IN ('landing', 'core')
   AND NOT c.relispartition
UNION ALL
SELECT 'core.daily_price',
       pg_size_pretty(sum(pg_total_relation_size(i.inhrelid))),
       sum(greatest(c.reltuples, 0))::bigint
  FROM pg_inherits i
  JOIN pg_class c ON c.oid = i.inhrelid
 WHERE i.inhparent = 'core.daily_price'::regclass
 ORDER BY 1;

\echo '-- core.daily_price bars by source (a full scan: seconds per 10M bars)'
SELECT source, count(*) AS bars
  FROM core.daily_price
 GROUP BY source
 ORDER BY bars DESC;

\echo '-- Sharadar files on disk, per the ledger'
SELECT vendor_table,
       kind,
       status,
       count(*)                         AS files,
       pg_size_pretty(sum(bytes))       AS bytes,
       sum(rows)                        AS rows,
       max(fetched_at)::date            AS newest
  FROM landing.sharadar_file
 GROUP BY vendor_table, kind, status
 ORDER BY vendor_table, kind, status;

SELECT pg_size_pretty(coalesce(sum(bytes), 0)) AS on_disk_per_ledger
  FROM landing.sharadar_file
 WHERE status <> 'deleted';
