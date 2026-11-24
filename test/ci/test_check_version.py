"""The version gate: every version that reaches main is a release.

`scripts/check_version.py` runs on every pull request (the `version` workflow),
because merging one publishes `v<__version__>` when it has no release yet (the
`release` workflow, ADR 0022). These tests pin which paths are what the host runs,
that a change to them needs a new number, that an added migration needs a minor
one unless it marks itself a patch, and that the version never goes down.
"""

from __future__ import annotations

import json
import subprocess

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "src/fafnir/cli.py",
        "src/duk/__init__.py",
        "sql/migrations/0047_dq_audit.up.sql",
        "scripts/daily_update.sh",
        "etc/systemd/fafnir-nightly.service",
        "pyproject.toml",
    ],
)
def test_what_the_host_runs_ships(check_version, path):
    assert check_version.ships(path)


@pytest.mark.parametrize(
    "path",
    [
        "doc/releasing.md",
        "README.md",
        "test/fafnir/test_version.py",
        ".github/workflows/release.yml",
        ".claude/skills/fafnir-dba/SKILL.md",
        "Makefile",
        "etc/fafnirrc",  # documentation, as the docs gate counts it
        "etc/crontab.example",
        "scripts/check_version.py",  # the repository's own tooling
        "scripts/check_docs_touched.py",
        "scripts/release.sh",
        "scripts/__pycache__/x.cpython-313.pyc",
        "srcfoo/x.py",
    ],
)
def test_the_rest_does_not(check_version, path):
    assert not check_version.ships(path)


def test_a_code_change_without_a_bump_fails_and_names_the_paths(check_version):
    passed, message = check_version.evaluate(
        "3.2.0", "3.2.0", ["src/fafnir/b.py", "src/fafnir/a.py", "doc/x.md"], {}
    )
    assert not passed
    assert "  src/fafnir/a.py\n  src/fafnir/b.py\n" in message
    assert "doc/x.md" not in message
    assert "scripts/release.sh" in message


@pytest.mark.parametrize("head", ["3.2.1", "3.3.0", "4.0.0"])
def test_any_raise_passes_a_code_change(check_version, head):
    passed, message = check_version.evaluate("3.2.0", head, ["src/fafnir/a.py"], {})
    assert passed
    assert f"v{head}" in message


def test_a_change_to_nothing_the_host_runs_keeps_the_version(check_version):
    passed, message = check_version.evaluate(
        "3.2.0", "3.2.0", ["doc/a.md", "test/x.py", "scripts/check_version.py"], {}
    )
    assert passed
    assert "stays" in message


def test_the_version_never_goes_down(check_version):
    passed, message = check_version.evaluate("3.2.0", "3.1.9", ["doc/a.md"], {})
    assert not passed
    assert "only go up" in message


@pytest.mark.parametrize("version", ["3.2", "v3.2.1", "3.2.1-dev", ""])
def test_the_version_is_three_numbers(check_version, version):
    passed, _ = check_version.evaluate("3.2.0", version, ["src/fafnir/a.py"], {})
    assert not passed


def test_a_migration_in_a_patch_fails(check_version):
    migrations = {"0048_x": "CREATE TABLE ops.x (id int);\n"}
    paths = ["sql/migrations/0048_x.up.sql", "sql/migrations/0048_x.down.sql"]
    passed, message = check_version.evaluate("3.2.0", "3.2.1", paths, migrations)
    assert not passed
    assert "0048_x" in message
    assert "scripts/release.sh minor" in message
    assert "-- release: patch" in message


def test_a_migration_in_a_minor_passes(check_version):
    migrations = {"0048_x": "CREATE TABLE ops.x (id int);\n"}
    passed, message = check_version.evaluate(
        "3.2.0", "3.3.0", ["sql/migrations/0048_x.up.sql"], migrations
    )
    assert passed
    assert "0048_x" in message


def test_a_migration_marked_patch_may_be_a_patch(check_version):
    migrations = {"0048_idx": "-- release: patch\nCREATE INDEX i ON ops.x (id);\n"}
    passed, _ = check_version.evaluate(
        "3.2.0", "3.2.1", ["sql/migrations/0048_idx.up.sql"], migrations
    )
    assert passed


def test_every_added_migration_must_be_marked_for_a_patch(check_version):
    migrations = {
        "0048_idx": "-- release: patch\nCREATE INDEX i ON ops.x (id);\n",
        "0049_t": "CREATE TABLE ops.t (id int);\n",
    }
    passed, message = check_version.evaluate(
        "3.2.0", "3.2.1", ["sql/migrations/0049_t.up.sql"], migrations
    )
    assert not passed
    assert "0049_t" in message and "0048_idx" not in message


# --------------------------------------------------------------------------- end to end


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _set_version(repo, version):
    (repo / "src" / "fafnir" / "__init__.py").write_text(f'__version__ = "{version}"\n')


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo at 3.2.0 on `base`, checked out on a feature branch."""
    _git(tmp_path, "init", "-q", "-b", "base")
    (tmp_path / "src" / "fafnir").mkdir(parents=True)
    _set_version(tmp_path, "3.2.0")
    (tmp_path / "src" / "fafnir" / "a.py").write_text("x = 1\n")
    (tmp_path / "sql" / "migrations").mkdir(parents=True)
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "a.md").write_text("# A\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    return tmp_path


def _commit(repo, message="change"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def test_main_fails_on_a_code_change_without_a_bump(check_version, repo, capsys):
    (repo / "src" / "fafnir" / "a.py").write_text("x = 2\n")
    _commit(repo)
    assert check_version.main(["--base", "base"]) == 1
    assert "  src/fafnir/a.py" in capsys.readouterr().out


def test_main_passes_once_the_version_is_raised(check_version, repo):
    (repo / "src" / "fafnir" / "a.py").write_text("x = 2\n")
    _set_version(repo, "3.2.1")
    _commit(repo)
    assert check_version.main(["--base", "base"]) == 0


def test_main_finds_an_added_migration(check_version, repo, capsys):
    (repo / "sql" / "migrations" / "0048_x.up.sql").write_text(
        "CREATE TABLE t (i int);\n"
    )
    _set_version(repo, "3.2.1")
    _commit(repo)
    assert check_version.main(["--base", "base"]) == 1
    assert "0048_x" in capsys.readouterr().out
    _set_version(repo, "3.3.0")
    _commit(repo)
    assert check_version.main(["--base", "base"]) == 0


def test_main_judges_against_the_base_as_it_is_now(check_version, repo):
    """The number to beat is the base's own: main may have moved since the fork."""
    (repo / "src" / "fafnir" / "a.py").write_text("x = 2\n")
    _set_version(repo, "3.2.1")
    _commit(repo)
    _git(repo, "checkout", "-q", "base")
    _set_version(repo, "3.2.1")
    (repo / "doc" / "a.md").write_text("# A\n\nreleased 3.2.1\n")
    _commit(repo, "another release")
    _git(repo, "checkout", "-q", "feature")
    assert check_version.main(["--base", "base"]) == 1


def test_main_reads_the_base_from_the_event(
    check_version, repo, tmp_path_factory, monkeypatch
):
    (repo / "src" / "fafnir" / "a.py").write_text("x = 2\n")
    _commit(repo)
    base = subprocess.run(
        ["git", "rev-parse", "base"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    event = tmp_path_factory.mktemp("event") / "event.json"
    event.write_text(json.dumps({"pull_request": {"base": {"sha": base}}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert check_version.main([]) == 1
