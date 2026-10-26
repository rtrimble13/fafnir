# Fundamentals

> **Stub.** #108 fills in this page: what the statement tables hold and how to read them point in
> time, the as-first-reported convention and what it does to growth factors, and the deferred
> research mode. For now, the design is in
> [plans/fundamentals-sf1.md](plans/fundamentals-sf1.md), the rules in
> [ADR 0018](adr/0018-bitemporal-fundamentals.md) and
> [ADR 0019](adr/0019-derived-metrics-not-vendor-metrics.md), the columns in the
> [data dictionary](data_dictionary.md#fundamentals-migration-0035-adr-0018), and the loader in
> [ingestion.md](ingestion.md#statements-from-sf1-90).

## How it is tested

Every fixture is synthetic: no Sharadar row is committed. The golden values are built from the
companies' own 10-K filings, which are public record, and every restated value in them is
invented.

| File | Kind | What it proves |
|---|---|---|
| `test/fafnir/test_fundamentals_map.py` | unit | The statement map types SF1's statement indicators and nothing else: no per-share value, conversion or vendor metric. Classes, signs and SQL types are as decided |
| `test/fafnir/test_fundamentals_schema.py` | integration | Migration 0035. The constraints make a look-ahead and an undated restatement unstorable. One version is in force per period and one row is current per version. The taxonomy equals the map, the grants hold, and the upgrade leaves existing rows byte-identical |
| `test/fafnir/test_fundamentals_loader.py` | unit | The loader's logic: a row's map, fiscal labels (September and June years), every `known_from` rule, share-term recovery with exact split ratios, the hash at 1,000-share precision, and routing (a changed first-reported row is a correction, a changed restated row a restatement). Also that a split after load and a re-run write nothing |
| `test/fafnir/test_fundamentals_ingest.py` | integration | `fafnir ingest fundamentals` against the ledger. A backfill on both timelines, then a re-run that writes nothing. A nightly restatement, correction and split. Quarantine, unlinked keys, merges and the command |
| `test/fafnir/test_fundamentals_integration.py` | integration | End to end (#91): the golden values, read point in time. NULL is kept apart from 0. Loading twice is identical, and one moved value restates. The upgrade and the first load leave every existing table byte-identical |

**Golden values** (`test_fundamentals_integration.py`):
- **AAPL FY2020:** revenue 274,515,000,000, known from the 10-K of 2020-10-30 and not the day
  before.
- **AAPL FY2015:** weighted shares 5,753,421,000 as filed. They are recovered from SF1's
  post-2020 terms through the 4-for-1 of 2020-08-31.
- **MSFT FY2015:** two versions. As filed (93,580,000,000) until the load day, and a restated
  version from it.

**Standing invariants**, asserted after every load in `test_fundamentals_ingest.py`:
- nothing is known before its period ended (`known_from >= fiscal_date`);
- every period's current versions chain, each `known_to` equal to the next `known_from`, with
  only the last one open.

The panel's look-ahead invariant (#100) and the AAPL split-era P/E band (ADR 0019) join them with
the mart.
