"""Stored path representations do not change with the filesystem."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bettermemory.attestation_paths import (
    LiteralPaths,
    ReferencePaths,
    compile_targets,
    interpret_path,
    record_paths,
    target_path,
    unverifiable_inputs,
)
from bettermemory.verify import commit_drift_anchor_paths


@pytest.mark.parametrize("suffix", ["", ":2", ":2-9", ":2:4", ","])
def test_reference_spellings_record_the_file_that_was_checked(
    tmp_path: Path, suffix: str
) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("x = 1\n")
    raw = "src/app.py" + suffix
    legacy, targets = compile_targets([raw], root=str(tmp_path))
    assert legacy == [raw]
    assert targets == {raw: "src/app.py"}


@pytest.mark.parametrize("name", ["routes/[slug]", "app/[locale]", "docs/v1."])
def test_a_literal_keeps_its_meaning_after_deletion(tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("literal\n")
    if target.name not in {entry.name for entry in target.parent.iterdir()}:
        pytest.skip("this filesystem normalizes the literal trailing dot")
    legacy, targets = compile_targets([name], root=str(tmp_path))
    assert legacy == [] and targets == {name: name}
    target.unlink()
    record = SimpleNamespace(verified_paths=legacy, verified_path_targets=targets)
    stored = record_paths(record)
    assert isinstance(stored, LiteralPaths)
    assert interpret_path(stored[0], mode="literal") == name


@pytest.mark.parametrize(
    "name",
    [
        "//server/share/x",
        "\\\\server\\share\\x",
        "https://example.invalid/x",
        "x\0y",
        "src\\..\\..\\evil\\x",
        "../outside",
    ],
)
def test_unsafe_names_are_never_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    def probed(path: str) -> bool:
        pytest.fail(f"unsafe name reached exists: {path}")

    monkeypatch.setattr("bettermemory.attestation_paths.os.path.exists", probed)
    monkeypatch.setattr("bettermemory.attestation_paths.os.lstat", probed)
    _legacy, targets = compile_targets([name], root=str(tmp_path))
    assert targets == {name: None}


def test_target_resolution_uses_the_root_path_flavour() -> None:
    assert target_path("src\\file.py", "C:\\work\\app") == "C:\\work\\app\\src\\file.py"
    assert target_path("src\\file.py", "/work/app") == "/work/app/src\\file.py"


@pytest.mark.parametrize("first_literal", [True, False])
def test_anchor_memo_distinguishes_literal_targets_from_legacy_references(
    first_literal: bool,
) -> None:
    body = "The loader reads its configuration once."
    literal = LiteralPaths(("src/app.py:2",))
    reference = ReferencePaths(("src/app.py:2",))
    for paths in (literal, reference) if first_literal else (reference, literal):
        expected = (
            ("src/app.py:2",) if isinstance(paths, LiteralPaths) else ("src/app.py",)
        )
        assert commit_drift_anchor_paths(body, paths) == expected


def test_restore_checks_targets_and_returns_their_original_input_keys() -> None:
    record = SimpleNamespace(
        verified_paths=[], verified_path_targets={"app.py:2": "app.py"}
    )

    def absent(paths: tuple[str, ...]) -> list[str]:
        assert isinstance(paths, LiteralPaths)
        assert paths == ("app.py",)
        return ["/root/app.py"]

    assert unverifiable_inputs(record, absent) == ["app.py:2"]


@pytest.mark.parametrize("failure", [errno.EACCES, errno.EIO, errno.ELOOP])
def test_an_unreadable_literal_never_becomes_a_different_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: int,
) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "app.py").write_text("reference\n")
    literal = folder / "app.py,"
    literal.write_text("literal\n")
    real_stat, real_lstat = os.stat, os.lstat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path) == str(literal):
            raise OSError(failure, "cannot inspect literal")
        return real_stat(path, *args, **kwargs)

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path) == str(literal):
            raise OSError(failure, "cannot inspect literal")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "lstat", lstat)
    legacy, targets = compile_targets(["src/app.py,"], root=str(tmp_path))
    assert legacy == [] and targets == {"src/app.py,": "src/app.py,"}
    legacy, targets = compile_targets(
        ["src/app.py,"], root=str(tmp_path), mode="reference"
    )
    assert legacy == ["src/app.py,"] and targets == {"src/app.py,": "src/app.py"}
