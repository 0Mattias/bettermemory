"""What `log verify --help` and the secret scan's remediation tell a user.

`log verify` checks the origin anchor, the record kept beside the key of
where this host's history of the store begins, and its two outcomes that
change the verdict (`origin_mismatch`, tampered; an origin `unverifiable`
for want of a key) must be in the help a user reads before trusting the
exit code. The secret scan's remediation must not read as a delete: the
signed log keeps every earlier version of a record after an edit, a
removal and a prune, so rotating the credential is the fix and every text
says so first.
"""

from __future__ import annotations

import sys

import pytest

from bettermemory.secret_scan import REMEDIATION


def _help(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv, "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    # argparse wraps at the terminal width: compare words, not lines.
    return " ".join(capsys.readouterr().out.split())


def test_log_verify_help_names_the_origin_check_and_its_outcomes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _help(["log", "verify"], monkeypatch, capsys)
    assert "the origin anchor" in text
    assert "origin_mismatch" in text
    assert "the origin reads unverifiable" in text
    assert "<store_id>.origin" in text


def test_the_log_group_help_names_the_origin_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "the origin anchor" in _help(["log"], monkeypatch, capsys)


@pytest.mark.parametrize("location", sorted(REMEDIATION))
def test_every_remediation_says_to_rotate_first_and_deletes_nothing(
    location: str,
) -> None:
    text = REMEDIATION[location]
    assert text.startswith("Rotate the credential at its issuer: that is the fix")
    assert "no command deletes the value" in text
    assert "signed log keeps every earlier version" in text
    assert "after an edit, a removal and a prune" in text
    assert "hard-delete" not in text
    assert "deletes whole sessions" not in text


def test_the_prune_steps_say_the_log_keeps_its_copy() -> None:
    assert "not the log's copy" in REMEDIATION["tombstones"]
    assert "not the log's copy" in REMEDIATION["episodes"]
