#!/usr/bin/env bash
# release.sh -- set the version a pull request releases. Every version that reaches
# main is a release (ADR 0022): merging the pull request publishes it.
#
# Usage:
#   scripts/release.sh                    the version here, on main, and the last
#                                         release, with what has changed since
#   scripts/release.sh patch              3.2.0 on main -> 3.2.1 on this branch
#   scripts/release.sh minor              3.2.0 on main -> 3.3.0
#   scripts/release.sh major              3.2.0 on main -> 4.0.0
#   scripts/release.sh 3.4.0              set it explicitly
#
#   --dry-run      say what would happen and change nothing
#
# On a branch, with the change that needs it. The bump counts from the version on
# main (origin/main), so running it again on the same branch gives the same number.
# It rewrites the one line that carries the number (src/fafnir/__init__.py; ADR
# 0011 says why it lives in source), checks it with scripts/check_version.py, the
# gate the pull request meets, and checks every version in the repository agrees.
# Commit it with the change. When the pull request merges, .github/workflows/
# release.yml installs the merge commit, checks it says this number, and tags it
# vX.Y.Z with the GitHub release. Nothing here commits, tags or pushes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="${REPO_ROOT}/src/fafnir/__init__.py"
MAIN_REF="origin/main"

DRY_RUN=0
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
        --push|--allow-patch)
            die "$1 is gone: a release is the merge of its pull request (ADR 0022).
       A migration that only fixes marks itself with a line '-- release: patch'." ;;
        -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)        die "unknown option $1 (try --help)" ;;
        *)         [[ -n "${BUMP}" ]] && die "give one version or bump, not two"
                   BUMP="$1" ;;
    esac
    shift
done

cd "${REPO_ROOT}"
git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository"

current_version() {
    sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${VERSION_FILE}"
}
version_at() {
    git show "$1:src/fafnir/__init__.py" 2>/dev/null | sed -n 's/^__version__ = "\(.*\)"$/\1/p'
}

CURRENT="$(current_version)"
[[ -n "${CURRENT}" ]] || die "no __version__ found in ${VERSION_FILE}"
LAST_TAG="$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || true)"
MAIN=""
if git rev-parse -q --verify "${MAIN_REF}" >/dev/null; then
    MAIN="$(version_at "${MAIN_REF}")"
fi

# ------------------------------------------------------- no argument: report
if [[ -z "${BUMP}" ]]; then
    note "this checkout   : ${CURRENT}"
    note "on main         : ${MAIN:-(no ${MAIN_REF}: git fetch origin main)}"
    note "last release    : ${LAST_TAG:-(none yet)}"
    note "deployed commit : $(git describe --tags --always --dirty 2>/dev/null || echo '(unknown)')"
    if [[ -n "${LAST_TAG}" ]]; then
        note "since ${LAST_TAG}    : $(git rev-list --count --first-parent "${LAST_TAG}..HEAD") commit(s) on the first parent"
        mapfile -t added < <(git diff --name-only --diff-filter=A "${LAST_TAG}" HEAD -- sql/migrations |
                             sed -n 's|^sql/migrations/\(.*\)\.up\.sql$|\1|p')
        if (( ${#added[@]} )); then
            note "new migrations  : ${added[*]}"
        fi
    fi
    note ""
    if [[ -n "${MAIN}" && "${CURRENT}" != "${MAIN}" ]]; then
        note "Merging this branch publishes v${CURRENT}."
    else
        note "A pull request that changes what the host runs sets its version on its"
        note "branch, and merging it publishes that release:"
        note "    scripts/release.sh {patch|minor|major|X.Y.Z}"
    fi
    exit 0
fi

# --------------------------------------------------------- compute the target
[[ -n "${MAIN}" ]] || die "no ${MAIN_REF} to count from: git fetch origin main"
[[ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]] ||
    die "on main. A version goes through a pull request like any change: branch first."

IFS='.' read -r BASE_MAJOR BASE_MINOR BASE_PATCH <<< "${MAIN}"
case "${BUMP}" in
    major) NEW="$((BASE_MAJOR + 1)).0.0" ;;
    minor) NEW="${BASE_MAJOR}.$((BASE_MINOR + 1)).0" ;;
    patch) NEW="${BASE_MAJOR}.${BASE_MINOR}.$((BASE_PATCH + 1))" ;;
    *)     NEW="${BUMP#v}" ;;   # tolerate a leading v on the argument
esac

[[ "${NEW}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "'${NEW}' is not X.Y.Z. Releases are three numbers; the tag adds the v."

# Refuse a version that is not an increase on main's. Sorting with -V puts the
# greater last.
GREATEST="$(printf '%s\n%s\n' "${MAIN}" "${NEW}" | sort -V | tail -1)"
if [[ "${NEW}" == "${MAIN}" || "${GREATEST}" != "${NEW}" ]]; then
    die "${NEW} does not come after ${MAIN}, the version on main. Versions only go up."
fi

# ----------------------------------------------------------- do it, and check
note "==> ${MAIN} on main  ->  ${NEW} on this branch (merging publishes v${NEW})"
(( DRY_RUN )) && note "    dry run: nothing will be changed"

if [[ "${NEW}" != "${CURRENT}" ]]; then
    note "--> Rewriting ${VERSION_FILE#"${REPO_ROOT}"/}"
    run sed -i "s/^__version__ = \".*\"$/__version__ = \"${NEW}\"/" "${VERSION_FILE}"
    if (( ! DRY_RUN )); then
        [[ "$(current_version)" == "${NEW}" ]] || die "the rewrite did not take"
    fi
else
    note "--> ${VERSION_FILE#"${REPO_ROOT}"/} already says ${NEW}"
fi

# The gate the pull request meets, on what is committed plus this rewrite: a
# migration in a patch is refused here rather than in CI. It reads the committed
# tree, so stash the rewrite into a throwaway commit object (no branch moves).
note "--> Checking the version rules against ${MAIN_REF} (scripts/check_version.py)"
if (( DRY_RUN )); then
    note "    would run: python3 scripts/check_version.py --base ${MAIN_REF}"
else
    TREE_COMMIT="$(git stash create 2>/dev/null || true)"
    python3 scripts/check_version.py --base "${MAIN_REF}" --head "${TREE_COMMIT:-HEAD}" || {
        git checkout -- "${VERSION_FILE}" 2>/dev/null || true
        die "the version rules refuse ${NEW}; reverted"
    }
fi

# The consistency test is the whole point of having one source of truth, so prove
# it here rather than finding out from CI.
note "--> Checking every version in the repository agrees"
# `python -m pytest`, never the bare `pytest` on PATH. The test imports fafnir, duk
# and the installed distribution metadata, so it has to run in the interpreter this
# project is installed into. A pytest installed by pipx or uv is its own isolated
# environment that cannot import the project at all, and reaching it here turns a
# passing bump into "ModuleNotFoundError: No module named 'duk'".
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
# guaranteed to mismatch here. The release workflow installs before it tests, so it
# still runs where it means something.
PYTEST_ARGS=(test/fafnir/test_version.py -q -m "not needs_fresh_install")

if (( DRY_RUN )); then
    note "    would run: ${PY:-python} -m pytest ${PYTEST_ARGS[*]}"
elif [[ -n "${PY}" ]]; then
    "${PY}" -m pytest "${PYTEST_ARGS[@]}" \
        || { git checkout -- "${VERSION_FILE}"; die "version consistency check failed; reverted"; }
else
    note "    SKIPPED: no interpreter here has both pytest and fafnir importable."
    note "    Install the project first:  pip install -e '.[dev]'"
    note "    The release workflow still checks this before it tags."
fi

note ""
(( DRY_RUN )) && { note "Dry run finished. Nothing changed."; exit 0; }
note "Commit src/fafnir/__init__.py with the change that needs it. Merging the pull"
note "request publishes v${NEW}: the release workflow tags the merge commit and writes"
note "the deploy steps into the release notes. On the warehouse host, then:"
note "    sudo -u fafnir -H git -C /opt/fafnir pull"
note "    sudo -u fafnir -H /opt/fafnir/.venv/bin/fafnir --version    # ${NEW}"
note "and, if the release adds migrations, the operator applies them:"
note "    sudo -u fafnir -H /opt/fafnir/.venv/bin/fafnir db migrate"
