"""No CLI path opens a backup bundle's file in place.

Opening a store, even read-only through `Store.open`, switches a file
saved in rollback-journal mode to WAL, which rewrites its header: a
bundle's snapshot then fails its signed hash, `backup verify` reads the
bundle mismatched and `backup restore` refuses it. A writing opener also
adds rows to it. The log readers and the secret scan refused a bundle's
file before; these tests pin the same refusal on every other command that
opens a store a user can point at (`BETTERMEMORY_DIR` naming a bundle, a
`--store`-like flag naming its file): each says to copy the file out
first, exits with its own code for a store it cannot read, and leaves the
bundle byte for byte as it was, so it still verifies. They also pin what
the guard does not take for a bundle: another tool's `manifest.json`
beside a store.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import _daemon_client, doctor
from bettermemory.backup import (
    bundle_manifest_beside,
    bundle_refusal,
    create_backup,
    restore_bundle,
    verify_bundle,
)
from bettermemory.cli import _common
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store


def _memory(body: str) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )


def _tree(*roots: Path) -> dict[str, bytes]:
    """Every file under `roots` and its bytes, to compare before and after."""
    found: dict[str, bytes] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                found[str(path)] = path.read_bytes()
    return found


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    """`bettermemory <argv>` in process: its exit code, 0 for a command
    that returns."""
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.fixture
def store_file(tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store with one memory, its key under the test's keys directory,
    resolved by the CLI through BETTERMEMORY_DIR."""
    directory = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(directory))
    path = directory / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as created:
        created.put_memory(_memory("alpha"))
    return path


@pytest.fixture
def bundle(store_file: Path, tmp_path: Path) -> Path:
    return Path(create_backup(store_file, out_dir=tmp_path / "bundles")["bundle"])


def _assert_untouched(bundle: Path, before: dict[str, bytes]) -> None:
    assert _tree(bundle) == before
    assert verify_bundle(bundle)["status"] == "ok"


# ---------------------------------------------------------------------------
# BETTERMEMORY_DIR naming a bundle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["export"], 2),
        (["export", "--mirror", "{tmp}/mirror"], 2),
        (["membership", "export"], 2),
        (["membership", "migrate", "--dry-run"], 2),
        (["health"], 1),
        (["serve"], 1),
        (["log", "verify"], 1),
        (["stats"], 2),
    ],
)
def test_a_command_refuses_a_bundle_as_its_store(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    code: int,
) -> None:
    """Each command's code for a store it cannot read, a message that says
    to copy the file out first, and a bundle that still verifies."""
    before = _tree(bundle)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(bundle))
    argv = [word.replace("{tmp}", str(tmp_path)) for word in argv]
    assert _run_cli(argv, monkeypatch) == code
    err = capsys.readouterr().err
    assert "backup bundle" in err
    assert "copy memory.sqlite out of the bundle" in err
    _assert_untouched(bundle, before)
    assert not (tmp_path / "mirror").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["session-start", "--session-id", "s1"],
        ["prompt-recall", "--prompt", "deploy steps", "--session-id", "s1"],
        ["audit-turn", "--session-id", "s1", "--dry-run"],
    ],
)
def test_an_in_process_hook_command_refuses_a_bundle_and_exits_0(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """A hook never fails its turn: exit 0, nothing on stdout (the
    model's context), the refusal on stderr, and the bundle unopened."""
    before = _tree(bundle)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(bundle))
    assert _run_cli(argv, monkeypatch) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "backup bundle" in captured.err
    _assert_untouched(bundle, before)


def test_the_writing_commands_context_refuses_a_bundle(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`cli_context` opens with `open_or_create`, which rekeys and writes;
    every writing command and `up --foreground` (the daemon) come here."""
    before = _tree(bundle)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(bundle))
    with pytest.raises(SystemExit) as exc:
        _common.cli_context()
    assert exc.value.code == 1
    assert "backup bundle" in capsys.readouterr().err
    _assert_untouched(bundle, before)


def test_the_shim_refuses_a_bundle_before_starting_a_daemon(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bare `bettermemory` refuses in one line rather than spawning a
    daemon that refuses, then falling back to an in-process server that
    refuses too."""
    from bettermemory import shim
    from bettermemory.cli import serve

    def never(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the shim went on past the refusal")

    monkeypatch.setattr(_daemon_client, "ensure_daemon", never)
    monkeypatch.setattr(serve, "run_serve", never)
    before = _tree(bundle)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(bundle))
    with pytest.raises(SystemExit) as exc:
        shim.run_shim()
    assert exc.value.code == 1
    assert "backup bundle" in capsys.readouterr().err
    _assert_untouched(bundle, before)


def test_doctor_fails_the_store_check_without_opening_the_bundle(
    bundle: Path,
) -> None:
    before = _tree(bundle)
    checks = doctor.check_store(doctor._Context(cwd=None, directory=bundle))
    store_check, key_check = checks
    assert store_check.id == "store"
    assert store_check.status == doctor.FAIL
    assert "backup bundle" in store_check.summary
    assert store_check.remedy is not None
    assert "copy memory.sqlite out of the bundle" in store_check.remedy
    assert store_check.details["bundle_manifest"] == str(bundle / "manifest.json")
    assert key_check.id == "key"
    assert key_check.status == doctor.WARN
    _assert_untouched(bundle, before)


def test_doctor_through_the_cli_exits_1_on_a_bundle(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exit code doctor gives any failed check, with the store check
    the one that names the bundle."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    before = _tree(bundle)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(bundle))
    assert _run_cli(["doctor", "--json"], monkeypatch) == 1
    document = json.loads(capsys.readouterr().out)
    checks = {check["id"]: check for check in document["checks"]}
    assert checks["store"]["status"] == "fail"
    assert "backup bundle" in checks["store"]["summary"]
    _assert_untouched(bundle, before)


def test_restore_refuses_a_store_inside_another_bundle(
    store_file: Path, bundle: Path, tmp_path: Path
) -> None:
    """A store resolved inside a bundle is that bundle's snapshot: the
    restore would move it aside and copy another bundle over it."""
    other = Path(create_backup(store_file, out_dir=tmp_path / "others")["bundle"])
    before = _tree(bundle, other)
    report = restore_bundle(bundle, other / STORE_FILENAME, apply=True)
    assert report["status"] == "refused"
    assert "backup bundle" in report["detail"]
    assert report["verify"] is None
    assert _tree(bundle, other) == before
    assert verify_bundle(other)["status"] == "ok"


def test_backup_restore_through_the_cli_exits_1_on_a_bundle_store(
    store_file: Path,
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = Path(create_backup(store_file, out_dir=tmp_path / "others")["bundle"])
    before = _tree(other)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(other))
    assert _run_cli(["backup", "restore", str(bundle), "--yes"], monkeypatch) == 1
    assert "backup bundle" in capsys.readouterr().out
    _assert_untouched(other, before)


# ---------------------------------------------------------------------------
# A flag naming the file
# ---------------------------------------------------------------------------


def test_export_mirror_store_flag_refuses_a_bundles_file(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _tree(bundle)
    argv = [
        "export",
        "--mirror",
        str(tmp_path / "mirror"),
        "--store",
        str(bundle / STORE_FILENAME),
    ]
    assert _run_cli(argv, monkeypatch) == 2
    assert "copy memory.sqlite out of the bundle" in capsys.readouterr().err
    _assert_untouched(bundle, before)


@pytest.mark.parametrize(
    "argv",
    [
        ["export", "--mirror", "{tmp}/mirror", "--store", "{link}"],
        ["migrate", "v8", "--from", "{tmp}/v8", "--to", "{link}"],
    ],
)
def test_a_named_file_with_other_names_is_refused(
    bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """A hard link to a bundle's snapshot names it from a directory with no
    manifest in it; a file named on the command line with other names is
    refused, as the secret scan's `--store` refuses it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = elsewhere / STORE_FILENAME
    try:
        link.hardlink_to(bundle / STORE_FILENAME)
    except OSError:
        pytest.skip("this account cannot create the link")
    (tmp_path / "v8").mkdir()
    before = _tree(bundle)
    words = [
        w.replace("{tmp}", str(tmp_path)).replace("{link}", str(link)) for w in argv
    ]
    code = _run_cli(words, monkeypatch)
    assert code == (2 if argv[0] == "export" else 1)
    assert "has other names (hard links)" in capsys.readouterr().err
    _assert_untouched(bundle, before)


@pytest.fixture
def linked_dir(bundle: Path, tmp_path: Path) -> Path:
    """A directory of its own whose `memory.sqlite` is a hard link to the
    bundle's snapshot: no manifest beside it, the bundle's bytes under it."""
    linked = tmp_path / "linked"
    linked.mkdir()
    try:
        (linked / STORE_FILENAME).hardlink_to(bundle / STORE_FILENAME)
    except OSError:
        pytest.skip("this account cannot create the link")
    return linked


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["export"], 2),
        (["membership", "export"], 2),
        (["health"], 1),
        (["episodes", "list"], 1),
        (["serve"], 1),
        (["log", "verify"], 1),
        (["stats"], 2),
        (["backup", "create"], 1),
        (["session-start", "--session-id", "s1"], 0),
    ],
)
def test_a_configured_store_that_is_a_hard_link_to_a_bundle_is_refused(
    bundle: Path,
    linked_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    code: int,
) -> None:
    """`BETTERMEMORY_DIR` (as `[storage] directory`, through the same
    resolution) names a directory whose store file is a hard link to a
    bundle's snapshot: every command refuses it, naming the path and
    saying to point the directory at a copy, and the bundle verifies."""
    before = _tree(bundle)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(linked_dir))
    assert _run_cli(argv, monkeypatch) == code
    err = capsys.readouterr().err
    assert f"{linked_dir / STORE_FILENAME} has other names (hard links)" in err
    assert "point BETTERMEMORY_DIR or `[storage] directory` at that directory" in err
    assert "`bettermemory key adopt --yes`" in err
    assert "`bettermemory backup restore BUNDLE --yes`" in err
    _assert_untouched(bundle, before)


def test_the_refusal_names_a_hard_linked_store_file_resolved_from_the_config(
    bundle: Path, linked_dir: Path
) -> None:
    link = linked_dir / STORE_FILENAME
    refusal = bundle_refusal(link)
    assert refusal is not None and refusal.startswith(f"{link} has other names")
    named = bundle_refusal(link, named=True)
    assert named is not None and "point the command at the copy" in named
    # A copy has one name, and is opened as before.
    link.unlink()
    link.write_bytes((bundle / STORE_FILENAME).read_bytes())
    assert bundle_refusal(link) is None


def test_the_restore_the_refusal_names_gives_a_store_that_writes(
    bundle: Path,
    linked_dir: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The configured store's remedy, followed: `backup restore BUNDLE
    --yes` on the hard-linked store moves the linked name aside (the bundle
    keeps its inode and verifies) and puts a file of its own there, which
    opens for writing. A copy alone, the remedy before, opened for reading
    only: the key ring stays bound to the original path (cold review M3)."""
    link = linked_dir / STORE_FILENAME
    refusal = bundle_refusal(link)
    assert refusal is not None
    assert "restore it in place from a bundle (`bettermemory backup restore" in refusal
    before = _tree(bundle)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(linked_dir))
    argv = ["backup", "restore", str(bundle), "--yes"]
    assert _run_cli(argv, monkeypatch) == 0, capsys.readouterr()
    capsys.readouterr()
    assert bundle_refusal(link) is None
    with Store.open(link, keys_dir=keys_dir) as opened:
        opened.put_memory(_memory("written once the link was broken"))
    _assert_untouched(bundle, before)


def test_migrate_v8_refuses_a_bundle_as_its_target(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--from` a bundle directory makes the bundle's snapshot the target
    (`memory.sqlite` inside the v8 directory): exit 1, the code for a
    store the migration cannot write."""
    before = _tree(bundle)
    assert _run_cli(["migrate", "v8", "--from", str(bundle)], monkeypatch) == 1
    assert "backup bundle" in capsys.readouterr().err
    _assert_untouched(bundle, before)


def test_a_symlink_to_a_bundles_file_is_refused(bundle: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = elsewhere / STORE_FILENAME
    try:
        link.symlink_to(bundle / STORE_FILENAME)
    except OSError:
        pytest.skip("this account cannot create the link")
    refusal = bundle_refusal(link)
    assert refusal is not None
    assert "backup bundle" in refusal


# ---------------------------------------------------------------------------
# What is not a bundle
# ---------------------------------------------------------------------------


def test_another_tools_manifest_beside_a_store_is_not_a_bundle(
    store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`manifest.json` is a common name, and the writing openers refuse a
    bundle too: a JSON document that does not name the bundle format
    must not stop the store from opening."""
    (store_file.parent / "manifest.json").write_text(
        json.dumps({"name": "an app", "version": 3}), encoding="utf-8"
    )
    assert bundle_manifest_beside(store_file) is None
    assert bundle_refusal(store_file, named=True) is None
    ctx = _common.cli_context()
    ctx.store.close()
    assert _run_cli(["export"], monkeypatch) == 0


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"format": "bettermemory.backup", "format_version": 1}),
        "{ not json",
        "[" * 100_000,
    ],
    ids=["bundle_format", "unparseable", "too_deep"],
)
def test_a_manifest_that_may_be_a_bundles_counts(
    store_file: Path, content: str
) -> None:
    """A manifest naming the format, and one that does not parse at all (a
    bundle whose manifest broke is still a bundle), both count."""
    manifest = store_file.parent / "manifest.json"
    manifest.write_text(content, encoding="utf-8")
    assert bundle_manifest_beside(store_file) == manifest


def test_a_store_with_one_name_is_not_refused_when_named(store_file: Path) -> None:
    assert bundle_refusal(store_file, named=True) is None
