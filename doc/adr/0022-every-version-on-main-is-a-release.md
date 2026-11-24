# ADR 0022: Every version on main is a release

- Status: Accepted
- Date: 2026-11-24
- Implemented by: `.github/workflows/release.yml` (publishes),
  `.github/workflows/version.yml` and `scripts/check_version.py` (the gate on a
  pull request), `scripts/release.sh` (sets the version on a branch; no longer
  commits, tags or pushes), `make bump`, `test/ci/test_check_version.py`
- Amends: [ADR 0011](0011-versioning.md): how a version becomes a release, and the
  tag. The number stays one constant in source, chosen by a person.
- Related: [doc/releasing.md](../releasing.md); rtrimble13/alpha-tango and dorq
  (dorq's ADR 0008), which release the same way

## Context

Under ADR 0011 a release was a person on `main` running `scripts/release.sh minor
--push`. That rewrote the constant, committed `Release vX.Y.Z` directly on `main`,
tagged it, and pushed both. `.github/workflows/version.yml` then checked the pushed
tag against the code. It worked when someone remembered to do it, from a machine
that could push to `main` and push tags. Between releases, merged changes waited
unreleased. On 2026-11-24, four pull requests had merged since v3.2.0 (#278 to
#281), with a migration (0047) among them. And nothing told the operator what a
pull would need, beyond `git log`.

rtrimble13/alpha-tango, and now dorq, release with no step by hand. A version on
main with no release is released, at the commit just pushed. A pull request that
changes what ships must raise the version above its base's, so the merge is the
release.

## Decision

1. **Every version that reaches main is a release.** A pull request that changes
   what the warehouse host runs raises `__version__` above its base branch's:
   `src/`, `sql/`, `scripts/`, `etc/` and `pyproject.toml`, less the repository's
   own tooling (`scripts/check_*.py`, `scripts/release.sh`) and the files under
   `etc/` that are documentation. `scripts/check_version.py`, run by the `version`
   workflow on every pull request, holds it to that. A pull request that changes
   nothing the host runs (docs, tests, CI) keeps the version, and its merge releases
   nothing.
2. **A migration is still not a patch.** A pull request that adds one raises the
   minor version at least, in the same gate. The old `--allow-patch` becomes a line
   in the migration itself, `-- release: patch`, for one that only fixes (an index,
   a constraint). The reviewer sees it in the diff, and the next reader sees it in
   the file.
3. **`scripts/release.sh` sets the version on a branch.** It counts from the
   version on `main`, so running it again on the same branch gives the same number.
   It checks the gate and the consistency tests, and leaves the commit to the
   author. It refuses to run on `main`. `--push` and `--allow-patch` are gone, and
   `make release` becomes `make bump`.
4. **The release workflow publishes.** On each push to `main` it reads
   `__version__`. If GitHub has no release of it, it installs the commit as the host
   does and checks that the source, the distribution metadata and all four CLIs say
   that number (what `version.yml` checked for a pushed tag). Then
   `gh release create vX.Y.Z --target <commit>` creates the tag and the release.
   The notes open with what the operator runs after the pull: `pip install -e` when
   `pyproject.toml` changed, and `fafnir db migrate` with the migrations by name.
   GitHub's list of the merged pull requests follows. A push whose version is
   released finishes in seconds. A run started by hand publishes from main only.
5. **The tag is made by the release**, as alpha-tango's is: a lightweight tag at
   the merge commit, not an annotated one. `git describe --tags`, which every check
   and doc here uses, reads both.

## Consequences

- Releasing is merging. Nobody commits to `main` by hand, and no credentials
  outside CI are needed to tag.
- fafnir releases more often: each pull request that changes what the host runs is
  a release. The host still upgrades when the operator pulls; a release says what
  is there to pull, and how.
- The bump is still a person's judgement, now made in review rather than at a
  terminal (ADR 0011, "Why not conventional commits"). Only the migration rule is
  mechanical, as before.
- The tag-guard of ADR 0011 (a tag pushed by hand that does not match its commit)
  becomes a step of the release workflow. A tag pushed by hand on another commit
  stops the release rather than being published.
- The first release under this ADR is 3.3.0. It carries what merged after v3.2.0,
  migration 0047 included, so it is a minor.
