# Schema map: which relation answers what

## Layers

```
landing  →  core  →  mart          ref / ops / meta cross-cut
  raw       truth    read seam     reference / lineage / migrations
```

Provenance flows one direction and everything downstream is rebuildable.
`landing.fmp_raw` holds immutable vendor payloads; `core` is the constrained
source of truth; `mart` is the read seam (ADR 0009) — **no client outside the
write path reads `core` or `ops` directly**, which is why `duk` names `mart`
throughout.

Two rules about `mart` that matter when you are tempted to add a view:

- **Adding a `mart` view is a grant** to every mart reader — every person, every
  agent, every app. It is a deliberate, reviewed act.
- **Never set `security_invoker = true`** on a `mart` view. Definer rights are the
  mechanism that lets a mart-only role read a core-derived relation; setting it
  silently breaks that, and silently because whoever writes it connects as a role
  that can read `core` anyway.

## Grains

| Relation | Grain |
|---|---|
| `core.security` | one row per listed security (`security_id`; keyed on `(source, symbol)`). `source` is the party that minted it; the FMP loaders feed `source = 'fmp'` rows only (`repo.FMP_FED_SECURITY`, ADR 0015) |
| `core.symbol_xref` | `(security_id, symbol, valid_from)` — ticker over time |
| `core.daily_price` | `(security_id, trade_date)`, partitioned by year, **raw** |
| `core.corporate_action` | `(security_id, action_type, ex_date)` |
| `core.adjustment_factor` | `(security_id, effective_date)` |
| `core.symbol_change` | the durable rename queue; `status` is terminal at `applied`/`ignored`/`dismissed` |
| `ops.ingestion_run` | one row per load |
| `ops.data_quality_flag` | `dq_flag_id`; one per open condition, except `price_*` |
| `ops.load_watermark` | `(source, endpoint, security_id)`; `security_id = 0` means whole-endpoint |
| `ops.operator_override` | `override_id`; one per operator edit, `reason` = `data_error`/`out_of_scope`/`cleanup` |
| `core.vendor_security` | `(vendor, vendor_table, vendor_key)`; a vendor's own master (Sharadar TICKERS), never written into `core.security` |
| `core.security_vendor_xref` | `(vendor, vendor_key, valid_from)`; open = `valid_to IS NULL`; one open link per key and per (security, vendor); `is_feed` = who loads new rows, one per security |
| `core.security_vendor_rejection` | `(security_id, vendor, vendor_key)`; a person's "not this key" (`vendor link reject`), which the linking engine never proposes again |
| `core.vendor_security_event` | `(vendor, vendor_key, event_date, action, contra_ticker)`; listings, delistings, renames, deals from ACTIONS |
| `ops.vendor_correction` | `correction_id`; one per row a higher-ranked vendor replaced (ADR 0015), `old_row` = the replaced row; active = `reverted_at IS NULL` |
| `ops.dq_label` | `label_id`; a label a person stated (`dq label`, `--label`); `security_id` NULL for a cohort (`members`) |
| `landing.fmp_raw` | `raw_id` — endpoint + symbol + `fetched_at` |
| `landing.sharadar_file` | `file_id`; one row per downloaded file (sha256 unique), the file itself on disk under `[sharadar] bulk_dir` |
| `meta.schema_migration` | `version` |

**What landing actually keeps.** `landing.fmp_raw` holds the payloads for prices,
dividends, splits, both corporate-action calendars and symbol changes, and from
v1.5.0 the delisted sweep's (`delisted-companies`, one row per run, `symbol` NULL).
It does **not** hold `stock-list` (the screener), and no delisted payload before
v1.5.0. So the screener cannot be audited after the fact, and **a join against
landing for a screener question, or a delisting before v1.5.0, returns nothing and
looks like a finding**. On 2026-09-10 that
produced a confident "173/173 off-screener" that was an artefact of the missing
rows. To ask whether the screener still lists a name, read
`core.security.updated_at`, which the nightly load stamps.

`landing.sharadar_file` is different again: it is a **ledger, not the data**. Each row
names a file under `[sharadar] bulk_dir` (`path` is relative to it), and the rows are
in that file, not in the database. So a question about what Sharadar said is
answered by reading the file (`fafnir.sources.sharadar_files.iter_rows`), and a
ledger row whose `status` is `deleted`, or whose file is missing after a restore,
has nothing behind it.

## The mart seam

| Relation | What it is | Live or lagged |
|---|---|---|
| `mart.v_daily_price_raw` | unadjusted OHLCV, as traded | live |
| `mart.v_daily_price_adjusted` | split/dividend adjusted, point-in-time stable | live |
| `mart.v_symbol_lookup` | ticker → `security_id` over time (passthrough of `symbol_xref`) | live |
| `mart.v_security_profile` | descriptive facts, one row per security | **live** |
| `mart.security_latest` | the screening contract | **materialized, lagged** |
| `mart.v_security_price_coverage` | span, bar count, close range, zero-volume bars | live — **use this, not a per-security probe** |
| `mart.v_security_action_summary` | split/dividend counts and boundaries, factor state | live |
| `mart.v_security_dq_open` | open flags: counts, keys, dates — **never `detail`** | live |

**`core.daily_price` is partitioned by year.** A per-security probe of it —
`min`/`max(trade_date)`, a bar count, a median — touches every partition and hits
the role's `statement_timeout`. Two ways round it, in order of preference: read
`mart.v_security_price_coverage`, which already holds first and last trade date,
bar count and zero-volume bars; or bound the query with `trade_date >= …` so the
planner can prune. An unbounded median volume times out; the same query with a
date bound returns.

The resolution ladder is **in the client**, not in `v_symbol_lookup` (which is a
passthrough, deliberately not a resolver): live ticker → primary symbol →
historical ticker. It is stated in `fafnir.db.repository.resolve_security_id` and
copied once, with a comment, in `duk.datasource.db`. **Do not add a third copy.**

## Privilege tiers

| Role | Reads | Writes |
|---|---|---|
| `fafnir_ingest` | everything | `landing`, `core`, `ref`, `ops` |
| `fafnir_read` | `core`, `mart`, `ref` | — |
| `fafnir_app` | `mart`, `ref` | — |
| `fafnir_ops` | `core`, `mart`, `ref`, `ops`, `landing`, `meta` | — |

`fafnir_ops` (migration 0021, ADR 0010) is the agent read tier. The agent holds
**no** writable role: mutations run the `fafnir` CLI as `fafnir_ingest` via
`sudo -u fafnir`. Per-person and per-agent login roles are members of these
groups and are deployment facts, not migrations.

The vendor identity tables (0031, and the rejections of 0032) are `core`, so they follow the tiers above:
`fafnir_read` and `fafnir_ops` read them, and `fafnir_app` does not, because no mart
view exposes them yet (#79). To see which Sharadar key a security is, or who feeds it:
`SELECT vendor, vendor_key, valid_from, valid_to, is_feed FROM
core.security_vendor_xref WHERE security_id = …`.

## Useful joins

```sql
-- flag → security → the bar it is about
SELECT s.primary_symbol, f.check_name, f.record_key, f.detail
  FROM ops.data_quality_flag f JOIN core.security s USING (security_id)
 WHERE f.resolved_at IS NULL;

-- flag → the load that wrote it
SELECT f.check_name, r.endpoint, r.status, r.started_at, r.error_message
  FROM ops.data_quality_flag f
  JOIN ops.ingestion_run r USING (ingestion_run_id)
 WHERE f.resolved_at IS NULL;

-- a security's coverage, actions and open flags in one row
SELECT p.symbol, c.first_trade_date, c.last_trade_date, c.bar_count,
       a.split_count, a.dividend_count,
       (SELECT count(*) FROM mart.v_security_dq_open d
         WHERE d.security_id = p.security_id) AS open_flags
  FROM mart.v_security_profile p
  LEFT JOIN mart.v_security_price_coverage c USING (security_id)
  LEFT JOIN mart.v_security_action_summary a USING (security_id)
 WHERE p.symbol = 'AAPL';
```
