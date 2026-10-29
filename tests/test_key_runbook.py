"""The recovery runbook and the refusals name commands, not files to
place or delete by hand (ENG-93 items 7 and 8).

The key ring's recoveries were hand edits of the keys directory: delete
`<store_id>.path` to hand the ring to another file, delete
`<store_id>.origin` and `<store_id>.head` to accept a replaced history,
file a key as `<store_id>.<first 16 hex of its fingerprint>.key`. ENG-106
ships each as a command that leaves a row in the store's log: `key adopt`,
`log accept-history`, `key import`. Pinned here: the remedy of every key
state that has one names its command and no deletion, `log verify`'s help
names `key import` and `log accept-history`, and the runbook
(`docs/installation.md`, `docs/operations.md`, `SECURITY.md`) asks for no
key-ring file to be placed or deleted by hand and names the commands.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from bettermemory import doctor, store_keys
from bettermemory.cli import main as cli_main
from bettermemory.log import KeyRing

_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ("docs/installation.md", "docs/operations.md", "SECURITY.md")
STORE_ID = "0123456789abcdef0123456789abcdef"

#: A hand edit of the keys directory, as the runbook used to ask for one.
HAND_EDITS = re.compile(
    r"delet\w*\s+(?:the\s+)?`<store_id>\.(?:path|origin|head)`"
    r"|`<store_id>\.<first 16 hex[^`]*`"
    r"|file it in the keys directory",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    ("state", "command"),
    [
        ("missing", "bettermemory key import FILE"),
        ("foreign", "bettermemory key import FILE"),
        ("bound_elsewhere", "bettermemory key adopt --yes"),
        ("unclaimed", "bettermemory log accept-history --yes"),
    ],
)
def test_each_remedy_names_its_command_and_no_deletion(
    state: str, command: str, tmp_path: Path
) -> None:
    ring = KeyRing(tmp_path / "keys", STORE_ID)
    read = store_keys.KeyState(
        state,
        ring,
        tmp_path / "store" / "memory.sqlite",
        "f" * 64,
        bound_to=tmp_path / "other" / "memory.sqlite"
        if state == "bound_elsewhere"
        else None,
    )
    remedy = read.remedy
    assert remedy is not None
    assert command in remedy
    assert "delete" not in remedy.lower()
    assert "put it back" not in remedy


def test_log_verify_help_names_the_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["bettermemory", "log", "verify", "--help"])
    with pytest.raises(SystemExit):
        cli_main()
    text = " ".join(capsys.readouterr().out.split())
    assert "bettermemory key import" in text
    assert "bettermemory log accept-history --yes" in text
    assert "Deleting" not in text


@pytest.mark.parametrize("name", (*RUNBOOK, "docs/api.md"))
def test_key_export_is_written_with_out(name: str) -> None:
    """`key export` takes its file as `--out FILE` (cold review M3)."""
    flat = " ".join((_ROOT / name).read_text(encoding="utf-8").split())
    bare = re.findall(r"key export(?! --out)(?!`)[^.;]{0,20}", flat)
    assert bare == [], bare


def test_doctors_head_remedies_name_the_commands(tmp_path: Path) -> None:
    """A head the log does not end at names `backup restore` and `log
    accept-history --yes`; an absent one asks for no copy by hand (cold
    review M4)."""
    head = tmp_path / f"{STORE_ID}.head"
    for tail in (None, (2, "mac")):
        finding = doctor._head_finding(
            head_seq=3, head_mac="mac", tail=tail, head_path=head
        )
        assert finding is not None and finding.remedy is not None
        assert "bettermemory backup restore" in finding.remedy
        assert "bettermemory log accept-history --yes" in finding.remedy
    mismatch = doctor._head_finding(
        head_seq=3, head_mac="mac", tail=(3, "other"), head_path=head
    )
    assert mismatch is not None and mismatch.remedy == doctor._ROLLBACK_REMEDY
    absent = doctor._head_finding(
        head_seq=None, head_mac=None, tail=(3, "mac"), head_path=head
    )
    assert absent is not None and absent.remedy is not None
    assert "copy" not in absent.remedy


def test_the_rc14_changelog_advises_no_binding_deletion() -> None:
    """The unreleased section names `key adopt`, not a binding to delete
    (cold review M4)."""
    text = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    rc14 = text[text.index("### 9.0.0rc14") : text.index("### 9.0.0rc13")]
    flat = " ".join(rc14.split())
    assert "deleting the `<store_id>.path` they name" not in flat


@pytest.mark.parametrize("name", RUNBOOK)
def test_the_runbook_asks_for_no_hand_edit_of_the_keys_directory(name: str) -> None:
    text = (_ROOT / name).read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert HAND_EDITS.search(flat) is None, HAND_EDITS.search(flat)
    for command in ("key adopt --yes", "key import", "log accept-history --yes"):
        assert command in flat, (name, command)
