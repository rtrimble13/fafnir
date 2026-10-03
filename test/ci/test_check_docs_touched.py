"""The docs gate: a behavioural change must bring its documentation with it.

`scripts/check_docs_touched.py` runs on every pull request (the `docs-gate`
workflow). These tests pin which paths demand docs, which paths count as docs,
that the `no-docs-needed` label is the one way out, and that a failure names the
paths that caused it -- a gate that only says "no" gets the label added blindly.
"""

from __future__ import annotations

import json
import subprocess

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "src/fafnir/cli.py",
        "sql/migrations/0027_sharadar_landing.sql",
        "scripts/daily_update.sh",
        "etc/systemd/fafnir-nightly.service",
    ],
)
def test_behavioural_paths_require_docs(docs_touched, path):
    assert docs_touched.requires_docs(path)
    assert not docs_touched.is_doc(path)


@pytest.mark.parametrize(
    "path",
    [
        "doc/operations.md",
        "doc/plans/sharadar-adoption.md",
        "README.md",
        ".claude/skills/fafnir-dba/SKILL.md",
        "etc/fafnirrc",
        "etc/crontab.example",
    ],
)
def test_documentation_paths_count_as_docs(docs_touched, path):
    assert docs_touched.is_doc(path)
    assert not docs_touched.requires_docs(path)


@pytest.mark.parametrize(
    "path",
    [
        "test/fafnir/test_config.py",
        ".github/workflows/lint.yml",
        "pyproject.toml",
        "doc.md",  # a prefix match on "doc" alone would wrongly count this
        "srcfoo/x.py",
        "test/README.md",  # only the top-level README is the README
    ],
)
def test_other_paths_are_neither(docs_touched, path):
    assert not docs_touched.requires_docs(path)
    assert not docs_touched.is_doc(path)


def test_src_only_fails_and_names_the_paths(docs_touched):
    passed, message = docs_touched.evaluate(
        ["src/fafnir/b.py", "src/fafnir/a.py", "test/x.py"], []
    )
    assert not passed
    assert "  src/fafnir/a.py\n  src/fafnir/b.py\n" in message
    assert "test/x.py" not in message
    assert "no-docs-needed" in message


def test_the_label_lets_it_pass(docs_touched):
    passed, message = docs_touched.evaluate(
        ["src/fafnir/a.py"], ["type:feature", "no-docs-needed"]
    )
    assert passed
    assert "src/fafnir/a.py" in message


def test_a_different_label_does_not(docs_touched):
    passed, _ = docs_touched.evaluate(["src/fafnir/a.py"], ["no-docs"])
    assert not passed


def test_touching_any_doc_satisfies_it(docs_touched):
    passed, message = docs_touched.evaluate(["sql/x.sql", "doc/data_dictionary.md"], [])
    assert passed
    assert "doc/data_dictionary.md" in message


def test_the_config_template_alone_is_a_doc_change(docs_touched):
    # etc/ is behavioural, but the annotated config template *is* the reference for
    # its keys: editing it is documenting them.
    passed, message = docs_touched.evaluate(["etc/fafnirrc"], [])
    assert passed
    assert "no docs required" in message


def test_no_behavioural_change_needs_no_docs(docs_touched):
    passed, _ = docs_touched.evaluate(["test/x.py", ".github/workflows/lint.yml"], [])
    assert passed


def test_an_empty_diff_passes(docs_touched):
    assert docs_touched.evaluate([], [])[0]


# --------------------------------------------------------------------------- end to end


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with one commit on `base`, checked out on a feature branch."""
    _git(tmp_path, "init", "-q", "-b", "base")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
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


def test_main_fails_on_a_src_only_branch(docs_touched, repo, capsys):
    (repo / "src" / "a.py").write_text("x = 2\n")
    _commit(repo)
    assert docs_touched.main(["--base", "base"]) == 1
    assert "  src/a.py" in capsys.readouterr().out


def test_main_passes_with_the_label(docs_touched, repo):
    (repo / "src" / "a.py").write_text("x = 2\n")
    _commit(repo)
    assert docs_touched.main(["--base", "base", "--label", "no-docs-needed"]) == 0


def test_main_passes_with_a_doc_change(docs_touched, repo):
    (repo / "src" / "a.py").write_text("x = 2\n")
    (repo / "doc" / "a.md").write_text("# A\n\nMore.\n")
    _commit(repo)
    assert docs_touched.main(["--base", "base"]) == 0


def test_moving_a_file_out_of_src_still_counts(docs_touched, repo, capsys):
    (repo / "lib").mkdir()
    (repo / "src" / "a.py").rename(repo / "lib" / "a.py")
    _commit(repo)
    assert docs_touched.main(["--base", "base"]) == 1
    assert "  src/a.py" in capsys.readouterr().out


def test_main_reads_commits_and_labels_from_the_event(
    docs_touched, repo, tmp_path_factory, monkeypatch
):
    (repo / "src" / "a.py").write_text("x = 2\n")
    _commit(repo)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    base = subprocess.run(
        ["git", "rev-parse", "base"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    def event(labels):
        path = tmp_path_factory.mktemp("event") / "event.json"
        pr = {
            "base": {"sha": base},
            "head": {"sha": head},
            "labels": [{"name": n} for n in labels],
        }
        path.write_text(json.dumps({"pull_request": pr}))
        return str(path)

    monkeypatch.setenv("GITHUB_EVENT_PATH", event([]))
    assert docs_touched.main([]) == 1
    monkeypatch.setenv("GITHUB_EVENT_PATH", event(["no-docs-needed"]))
    assert docs_touched.main([]) == 0
