"""Reference aliases of refused placeholders cannot become inert attestations."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bettermemory.attestation_paths import (
    PathMode,
    compile_targets,
    unverifiable_inputs,
)
from bettermemory.verify import unverifiable_attestations
from bettermemory import verify_candidates


@pytest.mark.parametrize("mode", ["auto", "reference", "literal"])
@pytest.mark.parametrize(
    "raw",
    [
        "$HOME/path/to/config.yaml",
        "${HOME}/path/to/config.yaml",
        "/foo/bar/",
        "/etc/foo/",
        "/etc/foo,",
    ],
)
def test_placeholder_alias_is_refused_instead_of_saved_as_an_inert_target(
    tmp_path: Path, raw: str, mode: PathMode
) -> None:
    legacy, targets = compile_targets([raw], root=str(tmp_path), mode=mode)
    record = SimpleNamespace(verified_paths=legacy, verified_path_targets=targets)
    bad = unverifiable_inputs(
        record,
        lambda paths: unverifiable_attestations(paths, worktree_root=tmp_path),
    )
    assert bad == [raw]


@pytest.mark.parametrize("name", ["foo,", "bar."])
def test_a_real_literal_punctuated_file_remains_attestable(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        verify_candidates,
        "_PLACEHOLDER_PATHS",
        {*verify_candidates._PLACEHOLDER_PATHS, str(tmp_path / "foo")},
    )
    path = tmp_path / name
    path.write_text("a literal file\n")
    raw = str(path)
    legacy, targets = compile_targets([raw], root=str(tmp_path))
    assert targets[raw] == raw
    record = SimpleNamespace(verified_paths=legacy, verified_path_targets=targets)
    assert (
        unverifiable_inputs(
            record,
            lambda paths: unverifiable_attestations(paths, worktree_root=tmp_path),
        )
        == []
    )
