# Releasing and upgrading

Fafnir is versioned `vX.Y.Z` and deployed by pulling a git checkout. Nothing is
published to PyPI, so a release is three things: a number that says what is running,
a tag that names the commit it came from, and a GitHub release whose notes say what
the operator runs after the pull.

**Every version that reaches main is a release**
([ADR 0022](adr/0022-every-version-on-main-is-a-release.md), as in
rtrimble13/alpha-tango and dorq). A pull request that changes what the host runs
raises the version, and merging it publishes the release. Nobody tags by hand.

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

## How a release happens

```bash
make version                      # this branch, main, and the last release
make bump BUMP=minor DRY_RUN=1    # what would happen
make bump BUMP=minor              # set this branch's version; then commit it
```

1. **In the pull request**, `scripts/release.sh` (`make bump`) sets the version
   the merge will release. It takes `patch`, `minor`, `major` or an explicit
   `X.Y.Z`, counted from the version on `main`. Running it again on the same branch
   gives the same number:

   | Command | 3.2.0 on main becomes |
   |---|---|
   | `scripts/release.sh patch` | 3.2.1 |
   | `scripts/release.sh minor` | 3.3.0 |
   | `scripts/release.sh major` | 4.0.0 |
   | `scripts/release.sh 3.4.0` | 3.4.0 |

   It rewrites the one line that carries the number. It checks the line against the
   gate below (`scripts/check_version.py --base origin/main`) and checks that every
   version in the repository still agrees. If either check fails, it puts the file
   back. It refuses to run on `main`, and it does not commit: commit
   `src/fafnir/__init__.py` with the change that needs it.
2. **The `version` workflow** checks every pull request
   (`.github/workflows/version.yml`, `scripts/check_version.py`):
   - The version is `X.Y.Z` and never goes down.
   - A change to what the host runs raises it above the base branch's: `src/`,
     `sql/`, `scripts/`, `etc/` or `pyproject.toml`, less the repository's own
     tooling (`scripts/check_*.py`, `scripts/release.sh`) and `etc/fafnirrc` and
     `etc/crontab.example`, which are documentation. A pull request that changes
     only docs, tests or CI keeps the version, and its merge releases nothing.
   - A pull request that adds a migration raises the minor version at least (below).
3. **The merge publishes it.** On each push to `main`, `.github/workflows/release.yml`
   reads `__version__`. If GitHub has no release of it, the workflow installs the
   commit as the host does. It checks that the source, the distribution metadata
   and all four CLIs say the number, then creates the tag `vX.Y.Z` at that commit
   and the GitHub release. The notes open with the deploy steps this release needs:
   `pip install -e` when `pyproject.toml` changed, and `fafnir db migrate` with the
   migrations by name. GitHub's list of the merged pull requests follows. A push
   that leaves the version as released finds nothing to do, in seconds. A run that
   failed can be run again from the Actions tab (`release`, "Run workflow", on
   `main`).

Never move or delete a pushed tag: a checkout that already has it will not pick up
a moved one. Fix forward with the next version.

**A migration is not a patch.** Each migration is an operator step after the pull
([Deploying it](#deploying-it)), so the number has to say one is there. A pull
request that adds one and raises only the patch number fails the gate, and
`release.sh patch` refuses it; `minor` is the usual answer ([Choosing the
number](#choosing-the-number)). A migration that only fixes (an index, a
constraint) and adds nothing a reader sees can say so with a line
`-- release: patch` in its `.up.sql`. When every migration a pull request adds says
so, a patch passes. That line replaces `release.sh --allow-patch`. v3.1.1, v3.1.2
and v3.1.8 each shipped a migration as a patch before the script checked.

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

## Which dorq

fafnir runs a dorq binary (`[dq] dorq_path`) and reads its output, so its code is
written against a dorq release. `DORQ_MIN_VERSION` in `src/fafnir/dq/dorq.py` is
the oldest it runs: every run reads `dorq --version` first and refuses an older
binary before it exports a row, and `fafnir status` marks one (`TOO OLD`).
[doc/dorq.md](dorq.md#install) installs a release at or above it, and
`test/fafnir/test_dorq_version.py` fails when the two disagree.

The minimum moves in the pull request that first reads a newer dorq's output, in
lockstep with that dorq release (dorq's CONTRIBUTING.md, "Callers move in
lockstep"): raise `DORQ_MIN_VERSION`, move the `version=` in doc/dorq.md's install
steps, and add an upgrade note here, since the operator installs the new dorq
before pulling the fafnir release that needs it.

dorq reports `X.Y.Z` only from its tagged release. A build of anything else (a
branch, or its main past a release) reports `X.Y.Z-dev`, and so does every flag it
writes
(`detail.dorq.version`). fafnir runs such a build when `X.Y.Z` meets the minimum,
with a warning in the log and `a development build, not a release` in `fafnir
status`. An audit cycle or a shadow night that names a `-dev` version did not run a
published dorq; the efficacy plan's measurements run releases.

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

`fafnir_mcp`, `fafnir_api` and `duk` re-export it, and `pyproject.toml` reads it through
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
- **Every pull request:** the `version` workflow (`scripts/check_version.py`)
  holds the version to the rules in [How a release happens](#how-a-release-happens).
- **Every push to main:** the `release` workflow publishes a version that has no
  release yet. First it installs the commit and refuses if the code, the metadata
  or any of the four CLIs disagrees with the number. A tag already there (pushed by
  hand) on another commit stops it too.

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
