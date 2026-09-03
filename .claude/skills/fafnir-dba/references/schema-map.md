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
| `core.security` | one row per listed security (`security_id`; keyed on `(source, symbol)`) |
| `core.symbol_xref` | `(security_id, symbol, valid_from)` — ticker over time |
| `core.daily_price` | `(security_id, trade_date)`, partitioned by year, **raw** |
| `core.corporate_action` | `(security_id, action_type, ex_date)` |
| `core.adjustment_factor` | `(security_id, effective_date)` |
| `core.symbol_change` | the durable rename queue; `status` is terminal at `applied`/`ignored`/`dismissed` |
| `ops.ingestion_run` | one row per load |
| `ops.data_quality_flag` | `dq_flag_id`; one per open condition, except `price_*` |
| `ops.load_watermark` | `(source, endpoint, security_id)`; `security_id = 0` means whole-endpoint |
| `landing.fmp_raw` | `raw_id` — endpoint + symbol + `fetched_at` |
| `meta.schema_migration` | `version` |

## The mart seam

| Relation | What it is | Live or lagged |
|---|---|---|
| `mart.v_daily_price_raw` | unadjusted OHLCV, as traded | live |
| `mart.v_daily_price_adjusted` | split/dividend adjusted, point-in-time stable | live |
| `mart.v_symbol_lookup` | ticker → `security_id` over time (passthrough of `symbol_xref`) | live |
| `mart.v_security_profile` | descriptive facts, one row per security | **live** |
| `mart.security_latest` | the screening contract | **materialized, lagged** |
| `mart.v_security_price_coverage` | span, bar count, close range, zero-volume bars | live |
| `mart.v_security_action_summary` | split/dividend counts and boundaries, factor state | live |
| `mart.v_security_dq_open` | open flags: counts, keys, dates — **never `detail`** | live |

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
