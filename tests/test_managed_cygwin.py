"""Cygwin and MSYS refuse a managed file another account could plant
(ENG-93 item 9).

Cygwin's and MSYS2's Python read ``/etc/bettermemory/managed.toml``, and
there ``/`` is a Windows folder that often inherits write access for
every account from ``C:\\``: the planting risk rc12 refused ProgramData
for. Five contracts, each pinned with the behaviour it promises:

1. **Refused, failing closed.** On a Cygwin or MSYS platform a managed
   file under a folder, from ``/`` down to the file, that an account other
   than an administrator can write stops the load with an error naming the
   file, the folder, why and what to do; `config check` reports it as an
   error. An absent file is still no managed config.
2. **Mounts another account can add.** Cygwin mounts what the
   world-writable ``/etc/fstab.d/<user>`` names, so a user mount on the way
   to the file is refused, a mount without access lists (``noacl``) too,
   and an unreadable ``/proc/mounts`` as well; a system mount and the
   cygdrive mounts are not.
3. **The chain as written and as resolved.** A link into a writable folder
   is refused like a file in one.
4. **What counts as writable.** Group or other write bits, or this
   account's own write access; a folder that cannot be checked is refused.
5. **Nothing changes elsewhere.** Linux, macOS, the BSDs and Windows never
   run the check.

The platform is faked through `managed._platform` and ``/proc/mounts``
through `managed._mounts_text`, so every test runs on every host; the
per-folder probe is faked where a test needs a folder no account but an
administrator can write, which a test cannot make.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from bettermemory import managed as _managed
from bettermemory.cli import config_cmd
from bettermemory.config import load_config

_PINS = "[telemetry]\nenabled = false\n"
#: POSIX only; bound once so `mypy --platform win32` sees no bare reference.
_GETEUID = getattr(os, "geteuid", None)
#: A default Cygwin install's mounts: the root a system mount, the drives
#: user mounts under /cygdrive.
_CYGWIN_MOUNTS = (
    "C:/cygwin64 / ntfs binary,auto 1 1\n"
    "C:/cygwin64/bin /usr/bin ntfs binary,auto 1 1\n"
    "C: /cygdrive/c ntfs binary,posix=0,user,noumount,auto 1 1\n"
)


def _user_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    return path


def _mount_point(path: Path) -> str:
    """`path` as ``/proc/mounts`` spells a mount point."""
    return path.as_posix().replace(" ", "\\040")


@pytest.fixture(autouse=True)
def mounts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """``/proc/mounts`` as a default Cygwin install lists it; a test adds
    a line by appending it."""
    lines = [_CYGWIN_MOUNTS]
    monkeypatch.setattr(_managed, "_mounts_text", lambda: "".join(lines))
    return lines


def _safe_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every folder as an administrator-only one, as a test cannot make."""
    monkeypatch.setattr(_managed, "_writable_by_users", lambda point: None)


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Select a platform by appending it: `managed._platform` answers the
    last one."""
    chosen = ["cygwin"]
    monkeypatch.setattr(_managed, "_platform", lambda: chosen[-1])
    return chosen


# ---------------------------------------------------------------------------
# 1. Refused, failing closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["cygwin", "msys"])
def test_a_managed_file_this_account_can_write_is_refused(
    tmp_path: Path,
    managed_config_path: Path,
    platform: list[str],
    name: str,
) -> None:
    """The test's own folder is this account's to write: on Cygwin or
    MSYS a managed file there is one any account like it could plant."""
    platform.append(name)
    managed_config_path.write_text(_PINS, encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(_user_config(tmp_path))
    message = str(caught.value)
    assert f"managed config {managed_config_path} is refused" in message
    assert "is writable by" in message
    assert "Let only administrators write" in message


def test_the_refusal_names_the_writable_folder_and_why(
    tmp_path: Path,
    managed_config_path: Path,
    platform: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The folder named is the first writable one from the root down."""
    managed_config_path.write_text(_PINS, encoding="utf-8")
    writable = managed_config_path.parent
    asked: list[Path] = []

    def probe(point: Path) -> str | None:
        asked.append(point)
        if point == writable:
            return "is writable by group or others (mode 0777)"
        return None

    monkeypatch.setattr(_managed, "_writable_by_users", probe)
    with pytest.raises(ValueError) as caught:
        load_config(_user_config(tmp_path))
    assert (
        f"managed config {managed_config_path} is refused: {writable} is "
        "writable by group or others (mode 0777)"
    ) in str(caught.value)
    assert asked[0] == Path(managed_config_path.absolute().anchor)
    assert asked[-1] == writable


def test_a_file_no_other_account_can_write_pins_as_before(
    tmp_path: Path,
    managed_config_path: Path,
    platform: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_config_path.write_text(_PINS, encoding="utf-8")
    asked: list[Path] = []

    def probe(point: Path) -> str | None:
        asked.append(point)
        return None

    monkeypatch.setattr(_managed, "_writable_by_users", probe)
    loaded = load_config(_user_config(tmp_path))
    assert loaded.telemetry.enabled is False
    assert loaded.managed_keys == frozenset({"telemetry.enabled"})
    # Every folder from the root down, then the file itself.
    assert managed_config_path.absolute() in asked
    assert managed_config_path.parent.absolute() in asked


def test_an_absent_file_under_a_writable_root_is_no_managed_config(
    tmp_path: Path, managed_config_path: Path, platform: list[str]
) -> None:
    assert not managed_config_path.exists()
    loaded = load_config(_user_config(tmp_path))
    assert loaded.managed_path is None
    assert loaded.managed_keys == frozenset()


def test_config_check_reports_the_refusal_as_an_error(
    managed_config_path: Path, platform: list[str]
) -> None:
    platform.append("msys")
    managed_config_path.write_text(_PINS, encoding="utf-8")
    report = config_cmd.check_report()
    assert report["status"] == "error"
    assert report["exit_code"] == config_cmd.EXIT_ERROR
    errors = [f for f in report["findings"] if f["level"] == "error"]
    assert [f["code"] for f in errors] == ["does_not_load"], errors
    assert f"managed config {managed_config_path} is refused" in errors[0]["message"]
    assert "is writable by" in errors[0]["message"]


@pytest.mark.parametrize(
    ("why", "elevated"),
    [
        ("is writable by this account", True),
        ("is writable by group or others (mode 0777)", False),
    ],
)
def test_a_refusal_for_this_accounts_write_access_names_an_elevated_process(
    tmp_path: Path,
    managed_config_path: Path,
    platform: list[str],
    monkeypatch: pytest.MonkeyPatch,
    why: str,
    elevated: bool,
) -> None:
    """An elevated process can write every folder, so letting only
    administrators write them cannot help it; the message says so."""
    managed_config_path.write_text(_PINS, encoding="utf-8")
    monkeypatch.setattr(_managed, "_writable_by_users", lambda point: why)
    with pytest.raises(ValueError) as caught:
        load_config(_user_config(tmp_path))
    message = str(caught.value)
    assert why in message
    assert "Let only administrators write" in message
    assert ("run bettermemory unelevated" in message) is elevated


# ---------------------------------------------------------------------------
# 2. Mounts another account can add
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "depth", ["the file's folder", "a folder above it", "through a link"]
)
def test_a_user_mount_on_the_way_is_refused(
    tmp_path: Path,
    managed_config_path: Path,
    platform: list[str],
    mounts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    depth: str,
) -> None:
    """What another account's line in /etc/fstab.d/<this user> mounts:
    refused though every folder on it reads as administrators' only, and
    when only the link's target lies under it."""
    managed_config_path.write_text(_PINS, encoding="utf-8")
    path, at = managed_config_path, managed_config_path.parent
    if depth == "a folder above it":
        at = at.parent
    if depth == "through a link":
        if sys.platform == "win32":
            pytest.skip("symlinks need a privilege")
        (tmp_path / "safe").mkdir()
        path = tmp_path / "safe" / "managed.toml"
        path.symlink_to(managed_config_path)
        monkeypatch.setattr(_managed, "managed_config_path", lambda: path)
        at = Path(os.path.realpath(managed_config_path.parent))
    mounts.append(f"C:/a/evil {_mount_point(at)} ntfs binary,user 0 0\n")
    _safe_folders(monkeypatch)
    with pytest.raises(ValueError) as caught:
        load_config(_user_config(tmp_path))
    message = str(caught.value)
    assert (
        f"managed config {path} is refused: {at} is under the mount "
        f"{at.as_posix()}, which has the user option: a user mount, which any "
        "account can add for another through /etc/fstab.d"
    ) in message
    assert "Remove the mount's line under /etc/fstab.d" in message


def test_config_check_names_the_user_mount_and_its_option(
    managed_config_path: Path,
    platform: list[str],
    mounts: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_config_path.write_text(_PINS, encoding="utf-8")
    at = managed_config_path.parent
    mounts.append(f"C:/a/evil {_mount_point(at)} ntfs binary,user 0 0\n")
    _safe_folders(monkeypatch)
    report = config_cmd.check_report()
    assert report["status"] == "error"
    errors = [f for f in report["findings"] if f["level"] == "error"]
    assert [f["code"] for f in errors] == ["does_not_load"], errors
    assert (
        f"is under the mount {at.as_posix()}, which has the user option"
        in errors[0]["message"]
    )


def test_a_mount_point_with_a_space_is_read(
    tmp_path: Path,
    platform: list[str],
    mounts: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/proc/mounts`` writes a space in a mount point as ``\\040``."""
    folder = tmp_path / "with space"
    folder.mkdir()
    managed = folder / "managed.toml"
    managed.write_text(_PINS, encoding="utf-8")
    monkeypatch.setattr(_managed, "managed_config_path", lambda: managed)
    mounts.append(f"C:/a/evil {_mount_point(folder)} ntfs binary,user 0 0\n")
    _safe_folders(monkeypatch)
    with pytest.raises(ValueError) as caught:
        load_config(_user_config(tmp_path))
    assert (
        f"{folder} is under the mount {folder.as_posix()}, which has the user option"
        in str(caught.value)
    )


def test_a_system_mount_and_the_cygdrive_mounts_are_not_refused(
    tmp_path: Path,
    managed_config_path: Path,
    platform: list[str],
    mounts: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an administrator writes /etc/fstab, and the drives' user
    mounts lie under /cygdrive (or /c on MSYS2), not on the way."""
    managed_config_path.write_text(_PINS, encoding="utf-8")
    mounts.append(
        f"D:/managed {_mount_point(managed_config_path.parent)} ntfs binary,auto 1 1\n"
        "C: /c ntfs binary,noacl,posix=0,user,noumount,auto 1 1\n"
    )
    _safe_folders(monkeypatch)
    assert load_config(_user_config(tmp_path)).telemetry.enabled is False


def test_a_mount_without_access_lists_is_refused_with_its_own_fix(
    tmp_path: Path,
    managed_config_path: Path,
    platform: list[str],
    mounts: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a noacl mount the mode is synthesised: no change to an access
    list can make it pass, so the message offers what can."""
    managed_config_path.write_text(_PINS, encoding="utf-8")
    at = managed_config_path.parent
    mounts.append(f"C:/msys64/x {_mount_point(at)} ntfs binary,noacl,auto 1 1\n")
    _safe_folders(monkeypatch)
    with pytest.raises(ValueError) as caught:
        load_config(_user_config(tmp_path))
    message = str(caught.value)
    assert (
        f"{at} is on the mount {at.as_posix()}, which has the noacl option: "
        "no access lists"
    ) in message
    assert "Mount it with acl, use a native Windows Python" in message
    assert "Let only administrators write" not in message


def test_an_unreadable_mount_table_is_refused(
    tmp_path: Path,
    managed_config_path: Path,
    platform: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_config_path.write_text(_PINS, encoding="utf-8")

    def unreadable() -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(_managed, "_mounts_text", unreadable)
    _safe_folders(monkeypatch)
    with pytest.raises(ValueError) as caught:
        load_config(_user_config(tmp_path))
    assert (
        f"managed config {managed_config_path} is refused: /proc/mounts cannot "
        "be read (Permission denied)"
    ) in str(caught.value)


# ---------------------------------------------------------------------------
# 3. The chain as written and as resolved
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege")
def test_a_link_into_a_writable_folder_is_refused(
    tmp_path: Path, platform: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    safe = tmp_path / "safe"
    writable = tmp_path / "writable"
    safe.mkdir()
    writable.mkdir()
    target = writable / "managed.toml"
    target.write_text(_PINS, encoding="utf-8")
    link = safe / "managed.toml"
    link.symlink_to(target)
    monkeypatch.setattr(_managed, "managed_config_path", lambda: link)
    resolved = Path(os.path.realpath(writable))

    def probe(point: Path) -> str | None:
        return "is writable by this account" if point == resolved else None

    monkeypatch.setattr(_managed, "_writable_by_users", probe)
    with pytest.raises(ValueError) as caught:
        load_config(_user_config(tmp_path))
    assert f"{resolved} is writable by this account" in str(caught.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX spelling of the path")
def test_the_chain_starts_at_the_root_as_written() -> None:
    points = _managed._planting_points(Path("/etc/bettermemory/managed.toml"))
    assert points[:4] == [
        Path("/"),
        Path("/etc"),
        Path("/etc/bettermemory"),
        Path("/etc/bettermemory/managed.toml"),
    ]
    assert len(points) == len(set(points))


# ---------------------------------------------------------------------------
# 4. What counts as writable
# ---------------------------------------------------------------------------


def test_group_or_other_write_bits_are_writable(tmp_path: Path) -> None:
    folder = tmp_path / "open"
    folder.mkdir()
    folder.chmod(0o777)
    why = _managed._writable_by_users(folder)
    assert why is not None
    if sys.platform != "win32":
        assert why == "is writable by group or others (mode 0777)"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_this_accounts_own_write_access_is_writable(tmp_path: Path) -> None:
    folder = tmp_path / "mine"
    folder.mkdir()
    folder.chmod(0o700)
    assert _managed._writable_by_users(folder) == "is writable by this account"


@pytest.mark.skipif(
    _GETEUID is None or _GETEUID() == 0,
    reason="needs a root-owned folder this account cannot write",
)
def test_a_folder_only_root_can_write_is_not() -> None:
    assert _managed._writable_by_users(Path("/")) is None


def test_a_folder_that_cannot_be_checked_is_refused(tmp_path: Path) -> None:
    why = _managed._writable_by_users(tmp_path / "absent")
    assert why is not None and why.startswith("cannot be checked")


# ---------------------------------------------------------------------------
# 5. Nothing changes elsewhere
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["linux", "darwin", "freebsd14", "win32"])
def test_other_platforms_never_check_the_folders(
    tmp_path: Path,
    managed_config_path: Path,
    platform: list[str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """The suite's file seam answers a path on every platform, Windows
    included, so the file is read there too; no folder is checked and no
    mount table read."""
    platform.append(name)
    managed_config_path.write_text(_PINS, encoding="utf-8")

    def probe(point: Path) -> str | None:
        raise AssertionError(f"checked {point} on {name}")

    def table() -> str:
        raise AssertionError(f"read /proc/mounts on {name}")

    monkeypatch.setattr(_managed, "_writable_by_users", probe)
    monkeypatch.setattr(_managed, "_mounts_text", table)
    assert load_config(_user_config(tmp_path)).telemetry.enabled is False
