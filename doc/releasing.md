# Releasing and upgrading

Fafnir is versioned `vX.Y.Z` and deployed by pulling a git checkout. Nothing is
published to PyPI, so a release is two things: a number that says what is running,
and a tag that names the commit it came from.

## Before anything merges: the docs gate

Documentation lands in the same PR as the behaviour it describes. The `docs-gate`
check (`.github/workflows/docs-gate.yml`) fails a pull request that changes
`src/`, `sql/`, `scripts/` or `etc/` without touching `doc/`, `README.md`,
`.claude/skills/`, `etc/fafnirrc` or `etc/crontab.example`, and it lists the paths
that required docs. When nothing a reader relies on changed (a refactor, a test
helper, a comment), add the `no-docs-needed` label: the check re-runs on its own
and passes. The same job runs `scripts/check_doc_links.py`, which fails on any
relative link or `#anchor` in the docs that does not resolve. Run both locally with
`python3 scripts/check_docs_touched.py --base origin/main` and
`python3 scripts/check_doc_links.py`.

The gate reports; it does not block. It is deliberately not a required status
check in branch protection (Sharadar plan v2), so a red `docs-gate` is a prompt to
add the docs or the label, and the reviewer decides.

## Cutting a release

```bash
make version                      # what is here now
make release BUMP=minor DRY_RUN=1 # what would happen
make release BUMP=minor           # do it
```

`scripts/release.sh` is the whole mechanism; the make targets just call it. It takes
`patch`, `minor`, `major`, or an explicit `X.Y.Z`:

| Command | 0.1.0 becomes |
|---|---|
| `scripts/release.sh patch` | 0.1.1 |
| `scripts/release.sh minor` | 0.2.0 |
| `scripts/release.sh major` | 1.0.0 |
| `scripts/release.sh 1.4.2` | 1.4.2 |

It rewrites the one line that carries the number, checks every version in the
repository still agrees, commits `Release vX.Y.Z`, and tags that commit. Then it
stops and prints the push command rather than pushing: a pushed tag is awkward to
take back, because a checkout that already has it will not pick up a moved one. Add
`--push` when you want it to finish the job.

Nothing is half-done if it refuses. It stops before touching anything when the
working tree is dirty, when you are not on `main`, when the tag already exists, or
when the new version does not come after the current one — and if the consistency
check fails after the rewrite, it puts the file back.

## Deploying it

```bash
sudo -u fafnir -H git -C /opt/fafnir pull
sudo -u fafnir -H /opt/fafnir/.venv/bin/fafnir --version
```

That is the whole upgrade for a version that changes only Python. Two cases need
more:

- **Dependencies changed** (`pyproject.toml` gained or moved a requirement) —
  `sudo -u fafnir -H /opt/fafnir/.venv/bin/pip install -e /opt/fafnir` as well.
- **Migrations were added** (`sql/migrations/` gained a file) — apply them:
  `sudo -u fafnir -H /opt/fafnir/.venv/bin/fafnir db migrate`. That is an operator
  command, never the agent's (ADR 0010).

Check the timers survived it — `scripts/monitor.sh` — and that the next nightly ran.

### Upgrade notes

**v1.5.0** adds migration `0028_labels_and_override_reasons`: the operator applies it
(`fafnir db migrate`) after the pull. It adds `ops.operator_override.reason`
(existing rows become `data_error`), `ops.data_quality_flag.judgement_basis` (marking
the closures whose note says they were closed by rule as `by_rule`: an UPDATE over the
closed flags, seconds on a queue of a million rows), and the table `ops.dq_label`.
`fafnir_ops` reads and `fafnir_ingest` writes the new table through the default
privileges of 0001 and 0021, as for every `ops` table. Until it is applied, every
command that reads the new columns fails; the nightly price load does not need it.

After it, by hand and once (dry run first; each is an operator-record edit an agent
may run on an explicit yes):

```bash
fafnir override set-reason --security-id 809739 --target daily_price \
    --reason out_of_scope -m "CELG-RI removed for scope 2026-10-14, not a data error" --dry-run
fafnir dq label import hand-20261015.jsonl --verified --dry-run
```

The release also changes what the nightly writes: a vendor restatement beyond
`[prices] restatement_max_ratio` / `restatement_max_volume_ratio` is now kept out and
flagged `price_restatement_conflict` instead of stored (doc/ingestion.md), and the
delisting sweep lands its payload. `fafnir source vendor-gaps` has a section in the
nightly report.

## Which number to trust

`fafnir --version` reads the source constant, so it is right the moment the pull
lands. Two other numbers can disagree with it, and both are answering a different
question:

| What you ran | What it tells you |
|---|---|
| `fafnir --version` | what the **code on disk** says. Correct after a bare `git pull`. |
| `git -C /opt/fafnir describe --tags` | which **commit** is checked out — `v0.2.0`, or `v0.2.0-3-gabc1234` if it is ahead of the tag |
| `pip show fafnir` | what pip recorded at the **last install**. Lags a pull until you reinstall. |

`describe` is the honest answer to "is the host on a release, or on something
in between?". A `-3-g…` suffix means three commits past the tag: not a release.

The `v` lives on the tag only. `fafnir.__version__` is `0.2.0`; the tag naming
that commit is `v0.2.0`. Python packaging metadata carries no prefix, and mixing
the two is how a version comparison silently stops working.

## Where the number lives

One place: `__version__` in `src/fafnir/__init__.py`.

`fafnir_mcp` and `duk` re-export it, and `pyproject.toml` reads it through
`[tool.setuptools.dynamic]`, so the distribution metadata cannot drift from the
source. `test/fafnir/test_version.py` asserts each of those links, including that
no second `__version__` literal has appeared anywhere in `src/`.

This was not always so. Four places carried the number and had already drifted —
`duk` said `1.1.0` while everything else said `0.1.0`, and this documentation
pinned both as expected output. Nothing noticed, because nothing looked.
[ADR 0011](adr/0011-versioning.md) records why the number is a source constant
rather than derived from the git tag.

## What CI enforces

- **Every push:** `test/fafnir/test_version.py` runs in the normal suite, so a
  reintroduced literal or a broken pyproject link fails a pull request.
- **Every `v*` tag:** `.github/workflows/version.yml` installs the tagged commit
  and refuses the tag if the code does not say what the tag says, or if any of the
  three CLIs disagrees. That is what catches a tag pushed by hand.

## Choosing the number

No changelog, no conventional-commit parsing — the commit messages in this
repository are prose that explains the production evidence for a change, and they
are worth more than a machine-readable prefix. Judge the bump by what a reader of
`git log` would call it:

- **patch** — a fix that changes no interface and no stored data.
- **minor** — a new command, flag or check; a loader that now writes something it
  did not before.
- **major** — anything that makes an existing invocation, a stored column, or a
  downstream read mean something different.

A migration is not automatically a major: adding a table is `minor`, changing what
an existing column means is `major`.
