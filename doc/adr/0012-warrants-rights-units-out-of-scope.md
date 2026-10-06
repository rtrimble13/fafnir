# ADR 0012: Warrants, rights and units are out of scope

- Status: Proposed
- Date: 2026-10-06
- Implemented by: `src/fafnir/ingest/security_master.py` (`instrument_kind`,
  `out_of_scope_kind`, `load_securities`), `src/fafnir/db/repository.py`
  (`security_footprint`, `purge_securities`), `fafnir security descope`,
  `[general] exclude_instruments`
- Related: [ADR 0005](0005-automatic-universe-maintenance.md) (the screener defines
  the universe), [ADR 0006](0006-curated-fund-universe.md) (the declared universe)

## Context

The screener lists every exchange-traded instrument on a US venue, so the universe
the nightly load maintains has included SPAC warrants, rights and units since the
first load. None of them is an equity. A warrant and a right are options on a listed
share; a unit is a share stapled to warrants or rights until it separates, usually
52 days after the IPO.

They cost far more to keep than they are worth. Measured against the production
DQ queue on 2026-10-06:

| | These three kinds | Everything else |
|---|---|---|
| Active securities | 615 (3.8%) | 15,506 |
| Bars, 2026-09-29..10-05 | 3.3% | 96.7% |
| Dollar volume, same week | ~0.03% | ~99.97% |
| Condition flags, 2026-09-08..10-06 | **31% (2,033 of 6,528)** | 69% |
| `stale` flags, same window | **82%** | 18% |
| Flags per active security, same window | 3.3 | 0.29 |

(The 615 also counts SPAC class A shares, which stay in scope. They carried 316 of
the 2,033 flags.)

Most of it is one mechanism. FMP publishes thin warrants, rights and units several
days late, so they go stale, take the late bar, close, and go stale again: 162 of
these securities raised three or more `stale` flags in four weeks. Units are the
worst case, with a median volume of one share a day. Each recurrence is operator
time, and none of it improves data anyone uses.

## Decision

1. **Classify by ticker.** `instrument_kind(symbol, name)` returns `warrant`,
   `right` or `unit`:
   - Nasdaq's reserved fifth letters W, R and U decide on their own.
   - So do the NYSE suffixes as FMP spells them (`-WT`, `-WTA`, `-WS`, `-RT`,
     `-UN`, …).
   - Two shapes need the vendor's name to name the same kind: a three-letter base
     plus W/R/U (`ZKPW`), and the second-series fifth letter Z (`ODVWZ`).

   Funds and ETFs never match. The name alone never decides: most SPAC warrants
   carry the sponsor's plain name, and "Units" also appears in MLP and equity-unit
   names that are in scope.
2. **Stop minting them.** `fafnir ingest securities` skips an excluded entry before
   anything is written, like an exchange test issue, and reports the count.
3. **Remove what is held** with `fafnir security descope`. It previews by default
   with `--dry-run`, deletes every row keyed to each security in one transaction,
   and leaves an `ops.ingestion_run` row naming every symbol removed.
4. **Exceptions are declared, not coded.** A symbol in `ref.tracked_symbol`
   (`fafnir track add`) is exempt from both steps. The known case is `GRP-UN`:
   Granite REIT's stapled units are its only US listing.
5. **Configurable.** `[general] exclude_instruments` defaults to all three kinds;
   `[]` restores the old behaviour. An unknown kind is a hard error, because a typo
   would otherwise silently re-admit a kind.

## Consequences

- **The history goes.** About 78k bars across 493 securities (2015-06 to 2026-10)
  are deleted, along with 38.7k DQ flags and 2.96k operator overrides. Only a
  backup restores them, so take one first.
- **Operating-company warrants and corporate rights go too** (for example `OPENW`
  and `SIFYR`), because the rule is about the instrument, not the issuer. Declare
  any you want to keep.
- **SPAC class A shares stay.** This decision does not cover them. Dropping them
  would buy little: they carried 4.8% of the flags, and FMP backfills a SPAC's
  trading history under the post-merger ticker anyway.
- **Rename records survive, detached.** `core.symbol_change` rows that point at a
  removed security have `security_id` set to NULL instead of being deleted, so the
  rename sweep does not re-offer them every night.
- **Landing payloads are kept.** `landing.fmp_raw` is the vendor archive and is not
  keyed to a security.
- **This does not delist anything.** Survivorship-free history remains the rule.
  `security descope` is for a scope decision, never for a security that stopped
  trading.

## Alternatives considered

- **Exempt these kinds from the `stale` check only.** That fixes the largest symptom
  but leaves the gaps (38% of them), the sparse-coverage flags (68%) and the
  bandwidth in place, and it makes the stale check say less.
- **Keep the securities but stop loading their prices.** That leaves inert rows in
  every screen of the historical universe and a second notion of "in scope" for
  every consumer to learn.
- **Delete with a SQL migration.** It has no dry run, no exemption and no audit
  row, and it runs whenever someone runs `fafnir db migrate` rather than when the
  operator decides.
