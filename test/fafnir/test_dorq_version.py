"""Which dorq fafnir runs: DORQ_MIN_VERSION, and the install steps that name it.

fafnir reads dorq's output, so its code is written against a dorq release. The
oldest it runs is :data:`fafnir.dq.dorq.DORQ_MIN_VERSION`; a run refuses anything
older before it exports a row. doc/dorq.md's install steps had pinned 0.8.0 long
after the statements run needed 0.13.0, and nothing noticed, because nothing
looked. These tests are what looks.
"""

from __future__ import annotations

import logging
import re
import stat
import sys
from pathlib import Path

import pytest

from fafnir.dq import dorq

REPO_ROOT = Path(__file__).resolve().parents[2]
DORQ_DOC = REPO_ROOT / "doc" / "dorq.md"


def test_versions_read_as_dorq_writes_them():
    assert dorq.DorqVersion.parse("0.14.0") == dorq.DorqVersion((0, 14, 0))
    dev = dorq.DorqVersion.parse("0.15.0-dev")
    assert dev == dorq.DorqVersion((0, 15, 0), "dev")
    assert not dev.is_release
    assert str(dev) == "0.15.0-dev"
    assert dorq.DorqVersion.parse("1.2.3+build.7") == dorq.DorqVersion((1, 2, 3))
    assert dorq.DorqVersion.parse(" 0.14.0\n") == dorq.DorqVersion((0, 14, 0))
    for text in ("", "0.14", "v0.14.0", "dorq 0.14.0", "0.14.0.1", "zero"):
        assert dorq.DorqVersion.parse(text) is None, text


@pytest.mark.parametrize(
    ("version", "ok", "warns"),
    [
        (dorq.DORQ_MIN_VERSION, True, False),
        ("0.14.0", True, False),
        ("1.0.0", True, False),
        ("0.12.9", False, False),
        ("0.8.0", False, False),
        # A build that is not a release: allowed when its own version meets the
        # minimum, with a warning; its flags name a version no archive has.
        ("0.15.0-dev", True, True),
        ("9.9.9-fake", True, True),
        ("0.12.0-dev", False, False),
        ("not a version", False, False),
    ],
)
def test_supported(version, ok, warns):
    allowed, message = dorq.supported(version)
    assert allowed is ok
    assert bool(message) is (warns or not ok)


def test_require_supported_refuses_an_old_dorq_and_warns_on_a_build(caplog):
    with pytest.raises(dorq.DorqError, match=r"older than the .* fafnir needs"):
        dorq.require_supported("0.12.0")
    with caplog.at_level(logging.WARNING):
        dorq.require_supported("0.15.0-dev")
    assert "a development build, not a release" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        dorq.require_supported(dorq.DORQ_MIN_VERSION)
    assert caplog.text == ""


def test_an_old_binary_is_refused_by_name(tmp_path):
    fake = tmp_path / "dorq"
    fake.write_text(f"#!{sys.executable}\nprint('0.10.0')\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    version = dorq.dorq_version(dorq.DorqSettings(path=str(fake)))
    assert version == "0.10.0"
    with pytest.raises(dorq.DorqError, match="0.10.0"):
        dorq.require_supported(version)


def _install_pin() -> str:
    pins = re.findall(r"^version=(\S+)$", DORQ_DOC.read_text(), flags=re.MULTILINE)
    assert (
        len(pins) == 1
    ), f"doc/dorq.md should name one dorq version to install, has {pins}"
    return pins[0]


def test_the_install_steps_name_a_release_fafnir_runs():
    """doc/dorq.md's `version=` is what an operator installs. It must be a release
    (no -dev), and at least the minimum: the steps are how a host meets it."""
    pin = dorq.DorqVersion.parse(_install_pin())
    assert pin is not None and pin.is_release, _install_pin()
    assert pin.release >= dorq.DorqVersion.parse(dorq.DORQ_MIN_VERSION).release, (
        f"doc/dorq.md installs dorq {pin}, older than DORQ_MIN_VERSION "
        f"{dorq.DORQ_MIN_VERSION}: move the install steps with the minimum"
    )


def test_the_install_section_states_the_minimum():
    text = DORQ_DOC.read_text()
    stated = re.findall(r"fafnir needs dorq (\d+\.\d+\.\d+) or later", text)
    assert stated == [dorq.DORQ_MIN_VERSION], (
        f"doc/dorq.md says fafnir needs {stated}; DORQ_MIN_VERSION is "
        f"{dorq.DORQ_MIN_VERSION}. Say it once, in Install, and keep it the constant's."
    )
