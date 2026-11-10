"""Home-relative targets use the host's canonical separators after expansion."""

from __future__ import annotations

import ntpath
from types import SimpleNamespace

import pytest

from bettermemory import attestation_paths


def test_expanded_windows_home_target_has_one_separator_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = r"C:\Users\Fixture"
    fake_path = SimpleNamespace(
        expanduser=lambda raw: root + raw[1:], normpath=ntpath.normpath
    )
    monkeypatch.setattr(attestation_paths, "os", SimpleNamespace(path=fake_path))
    raw = "~/proj/routes/[slug]"
    expected = root + r"\proj\routes\[slug]"
    assert attestation_paths.target_path(raw, None) == expected
    _legacy, targets = attestation_paths.compile_targets(
        [raw], root=root, mode="literal"
    )
    assert targets[raw] == expected
