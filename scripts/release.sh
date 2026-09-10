#!/usr/bin/env bash
# release.sh -- cut a version. Rewrites the one place the number is written,
# commits it, and tags the commit vX.Y.Z.
#
# Usage:
#   scripts/release.sh                    show the current version and the last tag
#   scripts/release.sh patch              0.1.0 -> 0.1.1
#   scripts/release.sh minor              0.1.0 -> 0.2.0
#   scripts/release.sh major              0.1.0 -> 1.0.0
#   scripts/release.sh 1.4.2              set it explicitly
#
#   --dry-run   say what would happen and change nothing
#   --push      push the branch and the tag when it is done (default: print
#               the command and let you run it)
#
# The version lives in src/fafnir/__init__.py; fafnir_mcp and duk re-export it and
# pyproject.toml reads it, so this script edits exactly one line. ADR 0011 says why
# it is a source constant rather than derived from the tag.
#
# Everything below refuses rather than guesses. Nothing is pushed unless you ask.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="${REPO_ROOT}/src/fafnir/__init__.py"
RELEASE_BRANCH="main"

DRY_RUN=0
PUSH=0
BUMP=""

die() { printf '%s\n' "error: $*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }
run() {
    if (( DRY_RUN )); then
        printf '    would run: %s\n' "$*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------- arguments
while (( $# )); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --push)    PUSH=1 ;;
        -h|--help) sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)        die "unknown option $1 (try --help)" ;;
        *)         [[ -n "${BUMP}" ]] && die "give one version or bump, not two"
                   BUMP="$1" ;;
    esac
    shift
done

current_version() {
    sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${VERSION_FILE}"
}

CURRENT="$(current_version)"
[[ -n "${CURRENT}" ]] || die "no __version__ found in ${VERSION_FILE}"

# ------------------------------------------------------- no argument: report
if [[ -z "${BUMP}" ]]; then
    note "current version : ${CURRENT}"
    note "last tag        : $(git -C "${REPO_ROOT}" describe --tags --abbrev=0 2>/dev/null || echo '(none yet)')"
    note "deployed commit : $(git -C "${REPO_ROOT}" describe --tags --always --dirty 2>/dev/null || echo '(unknown)')"
    note ""
    note "To cut one:  scripts/release.sh {patch|minor|major|X.Y.Z}"
    exit 0
fi

# --------------------------------------------------------- compute the target
IFS='.' read -r CUR_MAJOR CUR_MINOR CUR_PATCH <<< "${CURRENT}"
case "${BUMP}" in
    major) NEW="$((CUR_MAJOR + 1)).0.0" ;;
    minor) NEW="${CUR_MAJOR}.$((CUR_MINOR + 1)).0" ;;
    patch) NEW="${CUR_MAJOR}.${CUR_MINOR}.$((CUR_PATCH + 1))" ;;
    *)     NEW="${BUMP#v}" ;;   # tolerate a leading v on the argument
esac

[[ "${NEW}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "'${NEW}' is not X.Y.Z. Releases are three numbers; the tag adds the v."

TAG="v${NEW}"

# ------------------------------------------------------------------- refusals
# Each of these is a way a release goes wrong quietly, so each one stops.
cd "${REPO_ROOT}"

git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository"

if [[ -n "$(git status --porcelain)" ]]; then
    die "the working tree has uncommitted changes. A tag must name a commit that
       is exactly what was tested; commit or stash first."
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "${BRANCH}" != "${RELEASE_BRANCH}" ]]; then
    die "on branch '${BRANCH}', not '${RELEASE_BRANCH}'. Release from ${RELEASE_BRANCH}
       so the tag is on the history everyone else has."
fi

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
    die "tag ${TAG} already exists. Pick another version -- never move a tag that
       has been pushed, because a checkout that already has it will not update."
fi

# Refuse a version that is not an increase. Sorting with -V puts the greater last.
GREATEST="$(printf '%s\n%s\n' "${CURRENT}" "${NEW}" | sort -V | tail -1)"
if [[ "${NEW}" == "${CURRENT}" || "${GREATEST}" != "${NEW}" ]]; then
    die "${NEW} does not come after ${CURRENT}. Versions only go up."
fi

# ----------------------------------------------------------- do it, and check
note "==> ${CURRENT}  ->  ${NEW}   (tag ${TAG})"
(( DRY_RUN )) && note "    dry run: nothing will be changed"

note "--> Rewriting ${VERSION_FILE#"${REPO_ROOT}"/}"
run sed -i "s/^__version__ = \".*\"$/__version__ = \"${NEW}\"/" "${VERSION_FILE}"

if (( ! DRY_RUN )); then
    [[ "$(current_version)" == "${NEW}" ]] || die "the rewrite did not take; nothing committed"
fi

# The consistency test is the whole point of having one source of truth, so prove
# it before the commit rather than finding out from CI after the tag is pushed.
note "--> Checking every version in the repository agrees"
# `python -m pytest`, never the bare `pytest` on PATH. The test imports fafnir, duk
# and the installed distribution metadata, so it has to run in the interpreter this
# project is installed into. A pytest installed by pipx or uv is its own isolated
# environment that cannot import the project at all, and reaching it here turns a
# passing release into "ModuleNotFoundError: No module named 'duk'".
PY=""
for candidate in python python3; do
    if command -v "${candidate}" >/dev/null 2>&1 \
       && "${candidate}" -c 'import pytest, fafnir' >/dev/null 2>&1; then
        PY="${candidate}"
        break
    fi
done

# `needs_fresh_install` is deselected on purpose. That test reads the metadata pip
# wrote at install time, and we have just changed the constant pip read -- so it is
# guaranteed to mismatch here, and would revert every release. CI installs before it
# tests, so it still runs where it means something.
PYTEST_ARGS=(test/fafnir/test_version.py -q -m "not needs_fresh_install")

if (( DRY_RUN )); then
    note "    would run: ${PY:-python} -m pytest ${PYTEST_ARGS[*]}"
elif [[ -n "${PY}" ]]; then
    "${PY}" -m pytest "${PYTEST_ARGS[@]}" \
        || { git checkout -- "${VERSION_FILE}"; die "version consistency check failed; reverted"; }
else
    note "    SKIPPED: no interpreter here has both pytest and fafnir importable."
    note "    Install the project first:  pip install -e '.[dev]'"
    note "    CI still checks this, and the tag guard will reject a mismatch."
fi

note "--> Committing and tagging"
run git add "${VERSION_FILE}"
run git commit -m "Release ${TAG}"
run git tag -a "${TAG}" -m "Release ${TAG}"

# ----------------------------------------------------------------- push, or say
if (( DRY_RUN )); then
    note ""
    note "Dry run finished. Nothing changed."
    exit 0
fi

if (( PUSH )); then
    note "--> Pushing ${RELEASE_BRANCH} and ${TAG}"
    git push origin "${RELEASE_BRANCH}"
    git push origin "${TAG}"
    note ""
    note "Released ${TAG}."
else
    note ""
    note "Committed and tagged locally. Nothing has been pushed yet -- a pushed tag"
    note "is hard to take back, so that step is yours:"
    note ""
    note "    git push origin ${RELEASE_BRANCH} && git push origin ${TAG}"
    note ""
    note "To undo before pushing:  git tag -d ${TAG} && git reset --hard HEAD~1"
fi

note ""
note "Then on the warehouse host:"
note "    sudo -u fafnir -H git -C /opt/fafnir pull"
note "    sudo -u fafnir -H /opt/fafnir/.venv/bin/fafnir --version    # ${NEW}"
