# ADR 0007: Where the off-site backups live (Dropbox, evaluated)

- Status: **Proposed**
- Date: 2026-08-29
- Supersedes nothing; refines [install_hetzner.md §9](../install_hetzner.md#9-backups)
  and [operations.md § Backups](../operations.md#backups)
- Relevant to: [ADR 0006](0006-curated-fund-universe.md) (the declared universe is
  the part of the warehouse that cannot be re-fetched)

## Context

Today's backup story is a nightly `pg_dump` to `/var/backups/fafnir` with 14-day
retention, plus an optional `rsync` to a Hetzner Storage Box and Hetzner's own server
snapshots. That is three layers, but only one *vendor*: the server, the Storage Box
and the snapshots are all Hetzner. A billing lapse, an account suspension or a
credential compromise takes all three at once. The question on the table is whether
**Dropbox** should be where the off-site copy goes, on a fully automated weekly cycle
with a defined retention ("recycle") plan.

Two facts about fafnir shape every answer below, and both cut against the usual
database-backup instincts:

- **Most of the warehouse is reconstructible.** `landing` holds the raw FMP payloads
  and everything in `core` and `mart` is derived from them; losing a week of loads
  costs a re-run of idempotent, watermark-driven loaders, not data. A 7-day RPO would
  be negligent for a transactional system. Here it is defensible.
- **A small part of it is not reconstructible at all.** `ref.tracked_symbol` is a
  human's declared universe with the reason recorded (ADR 0006). `ops.data_quality_flag`
  carries resolutions and the operator's notes (migration 0017). `core.symbol_change`
  carries dismissals (migration 0018). `meta.schema_migration` is the ledger that stops
  `db migrate` re-applying everything. FMP will never hand these back. They are also
  tiny — kilobytes against gigabytes.

The valuable bytes and the numerous bytes are therefore not the same bytes, which is
the single most useful thing to know when choosing a target and a retention policy.

## Evaluation: Dropbox as the off-site target

### What genuinely argues for it

**It is a different failure domain.** This is the strongest argument and it is not a
small one. Storage Box is the cheaper, faster, better-integrated target — and it dies
with the same Hetzner account the server dies with. Dropbox is the only option on the
table that makes the off-site copy independent of the thing it is insuring against.

**The marginal cost is zero if the subscription already exists.** Dropbox Plus is
2 TB at $9.99/mo annual, Professional 3 TB at $16.58/mo; either swallows the entire
retention set without noticing. For comparison, the same data on Backblaze B2 at
$6/TB-month costs cents. Cost is not the deciding variable in either direction —
convenience and independence are.

**No transport ceiling to plan around.** Dropbox's documented data-transport limit
applies to Business plans (1 billion upload operations/month), not to individual Plus
or Professional accounts. Hetzner's outbound traffic is included. A weekly multi-GB
push costs nothing on either end.

**Version history is a second recycle layer you do not have to write.** 30 days on
Plus, 180 on Professional, plus Rewind. It sits *underneath* whatever pruning the
backup job does, which means a bug in our own retention logic is recoverable for a
month. Nothing in the current Storage Box `rsync` offers that.

**Restores do not require the toolchain.** A dump can be pulled from a phone in a
hotel with a password, no SSH key, no S3 client, no `hcloud` login. On the day the
server is gone this matters more than it sounds.

**The automation is boring.** `rclone` has a mature Dropbox backend and restic can
drive it through the rclone bridge, so nothing here is novel plumbing.

### What genuinely argues against it

**No immutability.** Dropbox has no object-lock or WORM equivalent. Anything holding a
valid token — or the account password — can delete the lot, and version history is
account-scoped and revocable by whoever took the account. B2 and Hetzner Object Storage
offer Object Lock plus application keys that can write but not delete. If ransomware
resistance is a design goal rather than a slogan, Dropbox alone does not deliver it.
This is the one demerit that cannot be engineered away from our side.

**It is a sync product, not a backup product, and that difference bites twice.**

1. *Do not install the Dropbox desktop client on the database host.* It is a
   bidirectional sync agent: it will pull the entire account down onto the server's
   disk, it wants an interactive login, and a deletion made anywhere propagates *to*
   the server. Use `rclone` against an **App folder**-scoped OAuth app, so the
   credential sitting on the server can only ever see `/Apps/fafnir-backup`.
2. *Never `rclone sync` to a backup target.* `sync` mirrors deletions; a bug that
   empties the local dump directory empties the remote too, and the whole point of the
   off-site copy is that it does not share fate with local state. Copy, then prune
   deliberately.

**Dropbox holds the keys.** There is no customer-managed key on individual plans, and
the dump is the entire warehouse. Client-side encryption is not optional here. restic
encrypts by default; `rclone crypt` or `age` would also do. The cost of that decision
is real and must be planned for: **a lost passphrase is a lost backup**, so the key
belongs somewhere that is neither the server nor Dropbox.

**The licence question is yours to check, not mine to assume.** Pushing bulk
FMP-derived data to a third-party consumer cloud touches whatever the FMP Professional
agreement says about storage and redistribution. Client-side encryption largely
defuses it — Dropbox stores ciphertext it cannot read — but read the clause.

**The access pattern needs tuning.** A restic repository is many medium files, and
Dropbox rate-limits an aggressive writer with HTTP 429 plus a `Retry-After` header.
Mitigated by large pack files (`--pack-size 64`) and modest concurrency
(`-o rclone.connections=4`), and by keeping `restic prune` — the listing-and-rewriting
step — off the weekly schedule. Not a blocker; it is the reason the weekly job and the
monthly job below are separate.

### The design mistake to avoid, which matters more than the vendor choice

The obvious implementation is to copy the existing `pg_dump -Fc -Z6` output to Dropbox
every Sunday. It works, and it is expensive in a way that quietly caps how long a
history can be kept: each week is an opaque ~2–3 GB blob sharing nothing with last
week's, so a year of weeklies is ~150 GB and every restore point costs full price.

But `core.daily_price`, `landing.fmp_raw` and `ops.ingestion_run` are essentially
append-only. Week over week, the overwhelming majority of the bytes are identical — and
a content-addressed backup tool captures that **only if we stop compressing first.**
Compression is what destroys the similarity: change one byte early in a compressed
stream and every byte after it differs, so deduplication finds nothing to share.

So: dump uncompressed and let the backup tool compress. `pg_dump -Fd -Z0 -j4`
(directory format, one file per table, parallel) into `restic backup --compression auto`.
An untouched table becomes a no-op; a grown table contributes only its new chunks.
For a 1990-start warehouse the first snapshot lands in the low single-digit GB and each
weekly delta in the low hundreds of MB — order **10–15 GB for a year of weekly restore
points instead of ~150 GB**. That is the difference between a retention policy that has
to be stingy and one that can afford a year of monthlies.

The price is scratch disk: an uncompressed directory dump is roughly the size of the
live database (~15–20 GB on the recommended 80 GB host — check before adopting; the
script refuses rather than filling the disk under PostgreSQL). Where disk is too tight,
`pg_dump -Fc -Z0 | restic backup --stdin` keeps the deduplication and needs no scratch
space, at the cost of per-table granularity and partial restore.

## Decision

**Adopt Dropbox as a second, vendor-independent off-site copy — not as the only one,
and not as the primary.** The layering that follows is standard 3-2-1, and Dropbox is
being used for the one thing it is uniquely good at here (independence from Hetzner)
rather than the things it is mediocre at (immutability, backup semantics).

| Layer | Target | Cadence | Purpose |
|---|---|---|---|
| 1 | `/var/backups/fafnir` local dump (§9.1, unchanged) | nightly | fast restore, yesterday's state |
| 2 | Hetzner Storage Box **or** B2 with Object Lock | weekly | primary off-site; B2 if immutability is wanted |
| 3 | **Dropbox**, App-folder scoped, via restic | weekly | independent of Hetzner entirely |
| 4 | Hetzner snapshots (§9.3) | before risky changes | whole-machine recovery |

Layers 2 and 3 are the same script (`scripts/backup_offsite.sh`) pointed at two
repositories; restic does not care which backend it is talking to. Running only layer 3
is an acceptable reduction given fafnir's reconstructibility — it accepts the
no-immutability and account-blast-radius risks knowingly.

### The recycle plan

Retention is declarative, and it is deliberately **two policies**, because the two
snapshot kinds have different value per byte:

```bash
# Weekly, full warehouse (large, mostly reconstructible)
--keep-last 4 --keep-weekly 8 --keep-monthly 12 --keep-yearly 2     # ~26 restore points

# Daily, ref + ops + meta only (tiny, irreplaceable)
--keep-daily 14 --keep-weekly 8 --keep-monthly 24
```

The full policy gives a month of dense cover, two months of weeklies, a year of
monthlies and two annual anchors — affordable only because of the deduplication
decision above. The state policy is more generous precisely because it is cheap: those
schemas are the operator's judgement calls, and re-deciding them is not possible at any
price. A daily state snapshot is the answer to the one real objection to a weekly
cycle.

Three scheduled jobs, all in `etc/crontab.example`:

- **Weekly** (Sunday, after the reconciliation run): dump → back up → `forget`.
- **Daily** (after `daily_update.sh`): `--state-only`, same two steps.
- **Monthly**: `--prune`, which reclaims the space `forget` unlinked and re-reads 5% of
  pack data (`restic check --read-data-subset=5%`). Separated because prune is the
  expensive, rate-limit-prone operation on Dropbox — and because an unverified backup
  is a hope, not a backup.

## Alternatives considered

- **Hetzner Storage Box only** (status quo, §9.2). Cheapest (€3.20/mo for 1 TB,
  unlimited traffic, 10+10 snapshots) and fastest to restore from, because it is in the
  same datacentre. Rejected *as the sole off-site copy* for exactly that reason: same
  vendor, same account, same blast radius. Kept as layer 2.
- **Backblaze B2** ($6/TB-month, free egress to 3× stored, Object Lock). Technically the
  best fit — WORM immutability and write-only application keys are things Dropbox
  cannot offer at any tier. Rejected as *the* answer only because it is a new
  subscription for a warehouse whose bulk is re-fetchable; it is the right upgrade the
  day immutability becomes a requirement, and the script already supports it.
- **The Dropbox desktop sync client on the server.** Rejected outright — see above; it
  is an anti-pattern that would replicate the account onto the database host and
  propagate deletions into it.
- **Plain `rclone copy` of `-Fc -Z6` dumps to Dropbox.** The simplest thing that works,
  and it does work. Rejected because it forecloses a long retention history for no
  saving, per the compression argument above.
- **Continuous archiving (WAL-E / pgBackRest / `archive_command`)** for a
  minutes-scale RPO. Rejected as disproportionate: the RPO that matters here is
  governed by re-ingestion cost, not by transaction loss, and this would add a
  standing operational burden for a benefit fafnir does not need.

## Consequences

- The off-site copy survives the loss of the Hetzner account, which today it does not.
- A dependency on `restic` and `rclone` on the host, and on an OAuth refresh token that
  can be revoked — a silent-failure mode the weekly job must be watched for, like any
  other cron job (`ops` is not involved; this is outside the warehouse).
- A restic passphrase becomes a piece of critical, non-recoverable secret material.
  Escrow it off-host and off-Dropbox before the first run, or the backups are decorative.
- The nightly local dump (§9.1) is unchanged and remains the fast path; nothing in this
  ADR touches the loaders, the schema or `daily_update.sh`.
- Immutability is explicitly *not* obtained. If that changes from nice-to-have to
  requirement, point layer 2 at B2 with Object Lock; no code changes.

## Implementation checklist

1. `apt install restic rclone` on the host.
2. Create a Dropbox app with the **App folder** permission (not Full Dropbox);
   `rclone config` a remote from it.
3. Generate a passphrase into `/etc/fafnir/restic.pass` (root-only, mode 0400) and
   **escrow a copy off-host**.
4. `FAFNIR_BACKUP_REPO=rclone:dropbox-fafnir: restic init`.
5. Dry-run `scripts/backup_offsite.sh --state-only`, then a full run.
6. Schedule the three jobs from `etc/crontab.example`.
7. Practise a restore into a scratch database, per
   [install_hetzner.md §9.4](../install_hetzner.md#94-practise-the-restore).
