# ADR 0011: One version, written in source, tagged `vX.Y.Z`

- Status: Accepted
- Date: 2026-09-10
- Implemented by: `src/fafnir/__init__.py`, `pyproject.toml`, `scripts/release.sh`,
  `test/fafnir/test_version.py`, `.github/workflows/version.yml`
- Related: [doc/releasing.md](../releasing.md),
  [ADR 0010](0010-on-host-operations-agent.md) (the deployed checkout is not a
  development checkout)

## Context

The repository had no tags and four version strings:

| Where | Value |
|---|---|
| `pyproject.toml` | `0.1.0` |
| `src/fafnir/__init__.py` | `0.1.0` |
| `src/fafnir_mcp/__init__.py` | `0.1.0` |
| `src/duk/__init__.py` | **`1.1.0`** |

They had already drifted. `duk` arrived in this repository with its own release
history and kept it, so `duk --version` said `1.1.0` while `pip show fafnir` said
`0.1.0` — for code that ships as one distribution, from one commit, installed by
one command. `doc/install_hetzner.md` pinned both as the expected output of a
post-install check, which made the drift look intentional.

The question a version answers here is narrow and operational. Nothing is published
to an index. There is one deployment: a `git clone` at `/opt/fafnir`, installed
editable into a venv, upgraded by `git pull`. So the version exists to answer
*"what code is running on the warehouse, and is it what I think I deployed?"* —
asked by an operator at a shell, and by a DQ triage session trying to tell whether a
defect it is looking at is already fixed.

## Decision

**One number, written in one place in source, re-exported by the other packages,
read by the build, and named by an annotated git tag with a `v` prefix.**

- `__version__` in `src/fafnir/__init__.py` is the only literal.
- `fafnir_mcp` and `duk` do `from fafnir import __version__`. `duk`'s independent
  `1.1.0` lineage ends here; it is not independently installable and never was.
- `pyproject.toml` declares `dynamic = ["version"]` and reads that attribute, so
  the distribution metadata is derived rather than repeated.
- `scripts/release.sh` performs a release. `make release BUMP=minor` calls it.
- The tag is `vX.Y.Z`; the Python version string is `X.Y.Z`. PEP 440 metadata
  carries no prefix.

## Why the number is in source, not derived from the tag

The obvious mechanism is `setuptools-scm`: tag `v0.2.0`, let the build derive the
version, never edit a constant. It is the right answer for a package published to
an index. It is the wrong answer here, and the reason is this deployment model.

A derived version is computed when **pip runs**. This warehouse is upgraded when
**git runs**. Measured on a checkout upgraded from `v0.1.0` to `v0.2.0` without
reinstalling:

| Source of the version | Reports |
|---|---|
| `importlib.metadata` (setuptools-scm) | `0.1.0` — stale |
| generated `_version.py` (setuptools-scm) | `0.1.0` — stale |
| a source constant | `0.2.0` — correct |
| `git describe --tags` | `v0.2.0` — correct |

`doc/install_hetzner.md` tells the operator to verify a deploy with
`fafnir --version`. Under setuptools-scm that check reports whenever pip last ran,
which is not the question being asked — and it fails *silently*, by printing a
plausible number. A version that is confidently wrong is worse than one that is
merely manual.

So the constant stays in source, where `git pull` updates it, and the automation
is pointed at the other end of the problem: keeping the tag, the constant and the
metadata in step.

The same staleness applies to `pip show fafnir`, which is why
`test_the_distribution_metadata_matches_the_source` is marked
`needs_fresh_install`: it compares what pip recorded against what the tree says,
which is an assertion about the *wiring* and only means anything when pip has run
since the constant last changed. CI installs before it tests, so there it always
does; `scripts/release.sh` deselects it for the one commit where a mismatch is
expected by construction.

## Why not conventional commits and semantic-release

Full automation would derive the bump from `feat:` / `fix:` subject prefixes. This
repository's commit messages are prose that states the production evidence for a
change — the queue counts, the affected tickers, what was ruled out. That is the
most valuable artefact the history has, and constraining subjects to a grammar so a
tool can pick a number would cost more than the number is worth on a repository
with one deployment target and no index. The bump is a judgement; `doc/releasing.md`
says how to make it.

## Consequences

- A release is one command and changes one line. `git log --oneline v0.1.0..HEAD`
  is the changelog; no separate file to keep honest.
- A tag pushed by hand that does not match its commit is rejected by CI rather than
  discovered on a deploy.
- Drift cannot reappear quietly: a second `__version__` literal anywhere in `src/`
  fails the test suite.
- `duk --version` goes *backwards*, from `1.1.0` to the repository version, once.
  That is a real cost, paid once, for a number that afterwards means something.
- The number still has to be chosen by a person. That is deliberate; see above.
- Anyone reading `pip show fafnir` on the host can see a stale number. The
  documented answer is `fafnir --version` for the code and `git describe` for the
  commit, and `doc/releasing.md` has the table.
