"""tools/src_delta.py: the net src/ count a unit's size prediction is graded on."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "src_delta.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("src_delta", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["src_delta"] = module
    spec.loader.exec_module(module)
    return module


src_delta = _load()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("1\n2\n3\n")
    (tmp_path / "README").write_text("x\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "tag", "base")
    (tmp_path / "src" / "a.py").write_text("1\n")
    (tmp_path / "src" / "b.py").write_text("1\n2\n3\n4\n5\n")
    (tmp_path / "README").write_text("x\ny\nz\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "unit")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_counts_only_src_and_nets_removals(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert src_delta.main(["base"]) == 0
    assert "2 files, +5/-2, net +3" in capsys.readouterr().out


def test_missed_if_exits_one_only_when_net_exceeds_the_bound(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert src_delta.main(["base", "--missed-if", "3"]) == 0
    assert "within" in capsys.readouterr().out
    assert src_delta.main(["base", "--missed-if", "2"]) == 1
    assert "MISSED" in capsys.readouterr().out
