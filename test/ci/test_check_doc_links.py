"""The documentation link check: every relative link and `#anchor` resolves.

`scripts/check_doc_links.py` runs in the `docs-gate` workflow. An anchor check is
only as good as its slugging, so the slug cases below are real headings from this
repository whose GitHub anchors are already linked from other docs -- if GitHub's
rule and ours ever disagree, these are the links that would break silently.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "heading, slug",
    [
        # Real headings (doc/data_dictionary.md, doc/install_hetzner.md, ADR 0008).
        (
            "`ops.data_quality_flag` — quarantine/anomaly queue. **Grain:** `dq_flag_id`.",
            "opsdata_quality_flag--quarantineanomaly-queue-grain-dq_flag_id",
        ),
        (
            "3.6 Password-free local auth for the nightly job (recommended)",
            "36-password-free-local-auth-for-the-nightly-job-recommended",
        ),
        (
            "4. The seam — `mart` is not yet complete, two views short",
            "4-the-seam--mart-is-not-yet-complete-two-views-short",
        ),
        # GitHub's rules, one at a time.
        ("Part A — Instructions", "part-a--instructions"),  # spaces are not collapsed
        ("snake_case_name", "snake_case_name"),  # underscores inside words stay
        ("_emphasis_ and *more*", "emphasis-and-more"),  # emphasis markers go
        ("[duk](duk.md) usage", "duk-usage"),  # a link keeps its text
        ("Café · Zürich", "café--zürich"),  # letters stay, symbols go
        ("✅ Done", "-done"),  # an emoji goes, its space stays
        ("Migrations (0027–0035)", "migrations-00270035"),  # en dash is not a hyphen
        ("<code>html</code> tags", "html-tags"),
    ],
)
def test_github_slug(doc_links, heading, slug):
    assert doc_links.github_slug(heading) == slug


def test_duplicate_headings_are_numbered(doc_links):
    text = "# Usage\n\n## Usage\n\n### Usage\n"
    assert {"usage", "usage-1", "usage-2"} <= doc_links.anchors(text)


def test_setext_headings_and_html_anchors_count(doc_links):
    text = 'Title\n=====\n\nSection\n-------\n\n<a name="custom-spot"></a>\n'
    assert {"title", "section", "custom-spot"} <= doc_links.anchors(text)


def test_a_list_item_above_a_rule_is_not_a_heading(doc_links):
    assert "item" not in doc_links.anchors("- item\n---\n")


def test_headings_inside_code_fences_do_not_count(doc_links):
    text = "```bash\n# not a heading\n```\n\n~~~\n## nor this\n~~~\n# Real\n"
    assert doc_links.anchors(text) == {"real"}


def test_links_are_found_outside_code_only(doc_links):
    text = (
        'See [a](a.md) and ![img](img/x.png "title").\n'
        "`[not](a-link.md)` in a code span.\n"
        "```\n[nor](this.md)\n```\n"
        "[ref]: ref.md\n"
        "A [wiki](https://en.wikipedia.org/wiki/Foo_(bar)) link.\n"
        "An [angle](<with space.md>) link.\n"
    )
    assert [t for _, t in doc_links.links(text)] == [
        "a.md",
        "img/x.png",
        "ref.md",
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        "<with space.md>",
    ]


@pytest.fixture
def docs(tmp_path):
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "b.md").write_text("# B\n\n## Some section\n")
    (tmp_path / "doc" / "img").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("")
    return tmp_path


def _check(doc_links, root, text):
    page = root / "doc" / "a.md"
    page.write_text(text)
    return doc_links.check_file(page, root, {})


@pytest.mark.parametrize(
    "link",
    [
        "[b](b.md)",
        "[b](b.md#some-section)",
        "[b](./b.md#B)",  # GitHub matches anchors case-insensitively
        "[self](#top)",
        "[dir](img/)",
        "[code](../src/x.py)",
        "[root](/src/x.py)",  # a leading slash is the repository root on GitHub
        "[site](https://example.com/missing.md#nowhere)",
        "[mail](mailto:someone@example.com)",
        '[b](b.md "B\'s title")',
        "[spaced](b%2Emd)",
    ],
)
def test_good_links_pass(doc_links, docs, link):
    assert _check(doc_links, docs, f"# Top\n\n{link}\n") == []


@pytest.mark.parametrize(
    "link, reason",
    [
        ("[x](missing.md)", "no such path"),
        ("[x](b.md#no-such-section)", "no heading '#no-such-section'"),
        ("[x](#nowhere)", "no heading '#nowhere'"),
        ("[x](../src/y.py)", "no such path"),
    ],
)
def test_broken_links_are_reported_with_file_and_line(doc_links, docs, link, reason):
    errors = _check(doc_links, docs, f"# Top\n\n{link}\n")
    assert len(errors) == 1
    assert errors[0].startswith("doc/a.md:3: broken link")
    assert reason in errors[0]


def test_main_scans_doc_readme_and_skills(doc_links, docs, capsys):
    (docs / "README.md").write_text("[doc](doc/b.md)\n")
    skill = docs / ".claude" / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("[gone](references/gone.md)\n")
    (docs / "doc" / "a.md").write_text("[b](b.md#some-section)\n")
    assert doc_links.main(["--root", str(docs)]) == 1
    out = capsys.readouterr().out
    assert ".claude/skills/s/SKILL.md:1: broken link 'references/gone.md'" in out
    assert "checked 4 Markdown files: 1 broken link(s)" in out


def test_the_repository_docs_have_no_broken_links(doc_links, capsys):
    assert doc_links.main([]) == 0, capsys.readouterr().out
