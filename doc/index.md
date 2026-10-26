# Fafnir Documentation Index

Fafnir is a research-grade financial market data warehouse on PostgreSQL + Python,
read through the `duk` CLI.

## Start here

- [README](../README.md) — overview, quick start, project layout
- [Architecture](architecture.md) — layers, ERD, role model, key decisions
- [Data Dictionary](data_dictionary.md) — every table/column: grain, source, units,
  adjustment status, cadence

## Operating it

- [Install on a Hetzner Cloud Server](install_hetzner.md) — end-to-end fresh install:
  provision, harden, PostgreSQL 16, fafnir, schedule, backups
- [Initial Setup & Backfill](backfill.md) — stand up the DB and backfill history
  (single run, a few hours); also
  [confirming the price feed](backfill.md#confirming-the-price-feed) and
  [clearing data for a reload](backfill.md#clearing-data-for-a-reload)
- [Ingestion](ingestion.md) — FMP endpoint→table map, idempotency, watermarks, the
  restatement guard, the adjustment step
- [Operations Runbook](operations.md) — setup, daily upkeep, monitoring,
  [working the DQ queue](operations.md#working-the-dq-queue),
  [venue transfers](operations.md#venue-transfers) ([ADR 0014](adr/0014-venue-transfer-is-not-a-delisting.md)),
  reconciliation, recovery, backups
- [dorq](dorq.md) — the Bayesian data-quality engine: install, configure, the
  shadow period, `dq compare`, labels (`dq export-labels`, `dq label`), calibration
  ([ADR 0013](adr/0013-dorq-dq-engine.md))
- [duk CLI](duk.md) — reading the warehouse (`db`, the default) or a vendor's API
  directly (`fmp`, `sharadar`); also
  [from Claude Code on a laptop](duk.md#from-claude-code-on-a-laptop): the socket
  tunnel, the agent role, and the Bash sandbox
- [Operations Agent](agent.md) — running an agent on the host for DQ triage,
  automation diagnosis and data questions: roles, MCP wiring, audit, revocation
- [Nightly Report Email](nightly_report.md) — the agent's morning summary of the
  automations and the DQ queue: schedule, mail transport, what it can and cannot see
- [Fundamentals](fundamentals.md) — SF1's statements on two timelines (ADR 0018); a stub
  for now, with how the build is tested (#91)
- [Releasing and Upgrading](releasing.md) — cutting a `vX.Y.Z` release with
  `make release`, deploying it to the host, and which of `--version`,
  `git describe` and `pip show` answers which question

## Growing it

- [Extending](extending.md) — add a source (FRED/BLS/BEA), fundamentals, MCP server
- Plans (proposed, not yet implemented):
  - [`duk ls <company>` company summary](plans/duk-company-summary.md) — per-company
    meta, price/action statistics, fundamentals seam, and open DQ flags
  - [Database-operations agent](plans/db-operations-agent.md) — an on-host Claude
    skill plus MCP server for DQ triage, automation assistance and data
    comprehension; the `fafnir_ops` read tier and the three-tier privilege model
    (**implemented** — see [agent.md](agent.md) and
    [ADR 0010](adr/0010-on-host-operations-agent.md))
  - [fafnir-dba skill enhancements](plans/fafnir-dba-skill-enhancements.md) — what
    the 2026-09-10 DQ session got wrong or had to derive from source, and the skill
    changes that followed: the flag-durability matrix, the environment check, the
    operator-directed bulk mode (**applied**)
  - [fafnir-dba skill enhancements, 2026-09-15](plans/fafnir-dba-skill-enhancements-2026-09-15.md)
    — what the outlier session got wrong or nearly got wrong, and the skill changes
    that followed: the outlier classification order, the plausibility gate, the
    survivorship check before deleting a segment, computed dry-run predictions
    (**applied**)
  - [fafnir-dba skill enhancements, 2026-10-22](plans/fafnir-dba-skill-enhancements-2026-10-22.md)
    — what closing out the Sharadar backfill (#76, #77, #79, #80, #81) taught the
    skill: the order that cleared 33,906 dead-history outliers, §2k's tests with
    thresholds, a scoped "already gone" set by id, and dorq `--full`'s effect on the
    shadow file (**applied**)
  - [Sharadar adoption](plans/sharadar-adoption.md) — the living plan, revised
    2026-10-18: Sharadar (Bundle, max history) becomes the system of record in one
    warehouse — it fills the history FMP never had and corrects what FMP has wrong —
    while FMP drops to a lower tier; six phases, the issue dispositions and the
    decision log; tracked in [#41](https://github.com/rtrimble13/fafnir/issues/41)
    (**in progress**)
  - [Sharadar COA assessment](plans/sharadar-coa-assessment.md) — the courses of
    action considered and why COA 2-M was chosen (reference)
  - [Market-data vendor assessment](plans/fafnir-vendor-assessment.md) — the vendor
    comparison the adoption plan cites (reference)
  - [Fundamentals on SF1](plans/fundamentals-sf1.md) — the fundamentals build plan,
    retargeted to Sharadar SF1: knowledge intervals, share terms, the panel (#85; signed
    off 2026-10-25)
  - [Fundamentals build prompt (FMP era)](plans/fundamentals-build-prompt-fmp.md) —
    the original fundamentals build prompt, retargeted to SF1 in SA-0801 (reference)
- Architecture Decision Records:
  - [ADR 0001 — Raw prices + adjustment factors](adr/0001-raw-prices-plus-adjustment-factors.md)
  - [ADR 0002 — Surrogate security_id & bitemporal readiness](adr/0002-surrogate-security-id-and-bitemporal-readiness.md)
  - [ADR 0003 — Postgres now, TimescaleDB later](adr/0003-postgres-now-timescale-later.md)
  - [ADR 0004 — Unadjusted price feed](adr/0004-unadjusted-price-feed.md)
  - [ADR 0005 — The universe maintains itself](adr/0005-automatic-universe-maintenance.md)
  - [ADR 0006 — A declared universe alongside the screened one (select mutual funds)](adr/0006-curated-fund-universe.md)
  - [ADR 0007 — Corporate actions load incrementally (market-wide calendar sweep)](adr/0007-incremental-corporate-actions.md)
  - [ADR 0008 — Remote `duk` under per-person credentials, and a local stdio MCP server](adr/0008-remote-duk-access-and-mcp.md)
  - [ADR 0009 — `mart` is the whole read seam, and definer-rights views are how](adr/0009-mart-is-the-read-seam.md)
  - [ADR 0010 — An on-host operations agent: a fourth read tier, and mutations stay on the CLI](adr/0010-on-host-operations-agent.md)
  - [ADR 0011 — One version, written in source, tagged `vX.Y.Z`](adr/0011-versioning.md)
  - [ADR 0012 — Warrants, rights and units are out of scope](adr/0012-warrants-rights-units-out-of-scope.md)
  - [ADR 0013 — dorq, a Bayesian DQ engine, beside the SQL checks](adr/0013-dorq-dq-engine.md)
  - [ADR 0014 — A venue transfer is not a delisting](adr/0014-venue-transfer-is-not-a-delisting.md)
  - [ADR 0015 — Multi-vendor provenance, precedence and audited corrections](adr/0015-multi-vendor-provenance-and-precedence.md)
  - [ADR 0016 — Sharadar's permaticker is the identity authority; survivorship floor 1998](adr/0016-sharadar-identity-and-survivorship-floor.md)
  - [ADR 0017 — Vendor access is one library inside fafnir, not a distribution per vendor](adr/0017-vendor-access-is-one-library-not-a-distribution-per-vendor.md) *(proposed)*
  - [ADR 0018 — Bitemporal fundamentals on Sharadar SF1](adr/0018-bitemporal-fundamentals.md)
  - [ADR 0019 — fafnir derives its metrics; vendor metrics are not read](adr/0019-derived-metrics-not-vendor-metrics.md)

## Reference

- Configuration template: [`etc/fafnirrc`](../etc/fafnirrc)
- Cron schedule: [`etc/crontab.example`](../etc/crontab.example)
- systemd unit templates: [`etc/systemd/`](../etc/systemd/)
- Migrations: [`sql/migrations/`](../sql/migrations/)
- Batch scripts: [`scripts/`](../scripts/)
- Agent configuration templates: [`etc/agent/`](../etc/agent/)
- Agent skill (DQ playbooks, data semantics): [`.claude/skills/fafnir-dba/`](../.claude/skills/fafnir-dba/)
