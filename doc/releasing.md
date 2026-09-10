# Releasing and upgrading

Fafnir is versioned `vX.Y.Z` and deployed by pulling a git checkout. Nothing is
published to PyPI, so a release is two things: a number that says what is running,
and a tag that names the commit it came from.

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
