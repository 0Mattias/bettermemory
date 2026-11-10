"""Native Linux byte directories reach identity, storage, and folder lookup."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bettermemory import identity, location, origin
from bettermemory.config import load_config
from bettermemory.membership import Membership
from bettermemory.store import Store

from .conftest import shielded_child_env
from .test_membership_cli import Scene, bm, scene
from .test_membership_folder import _KINDS, _document
from .test_store import _memory
from .test_identity import _call, _server
from ._mcp import fake_ctx

__all__ = ["scene"]

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="native Linux byte filenames"
)


def _repository(tmp_path: Path) -> str:
    raw = os.fsencode(tmp_path) + b"/bytes-\xff"
    os.mkdir(raw)
    env = shielded_child_env()
    for args in (
        ("init", "-q"),
        (
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "one",
        ),
    ):
        subprocess.run(["git", *args], cwd=raw, env=env, check=True, timeout=30)
    return os.fsdecode(raw)


def test_native_byte_repository_captures_and_persists_its_actual_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _repository(tmp_path)
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "store"))
    config = load_config()
    with Store(config.resolved_directory()) as store:
        model = Membership.open(store, location.process_machine())
        result = location.resolve_here(store, config, folder, session=None)
        assert result.home is not None and result.checkout is not None
        checkout = model.checkout(result.checkout)
        assert os.fsencode(checkout.views[-1].path) == os.fsencode(folder)
        assert checkout.git_dir is not None
        assert os.fsencode(checkout.git_dir) == os.fsencode(folder) + b"/.git"
        captured = origin.capture(Path(folder))
        assert captured.worktree_root == folder
        assert origin.repo_toplevel(Path(folder)) == Path(folder)
        memory = store.put_memory(
            _memory("native checkout origin", origin=captured), home=result.home
        )
        assert store.get_memory(memory.id).origin == captured
        assert store.log_verify()["status"] == "ok"
        assert store.refold()["status"] == "ok"


def test_native_byte_cli_declaration_succeeds(tmp_path: Path) -> None:
    folder = _repository(tmp_path)
    env = shielded_child_env()
    env["BETTERMEMORY_DIR"] = str(tmp_path / "store")
    result = subprocess.run(
        [sys.executable, "-m", "bettermemory", "project", "new"],
        cwd=os.fsencode(folder),
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"UnicodeEncodeError" not in result.stderr
    result.stdout.decode("utf-8")


@pytest.mark.parametrize("kind", _KINDS)
def test_each_question_kind_returns_the_exact_stored_native_folder(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    folder = scene.repo(os.fsdecode(b"bytes-\xff"), commits=0)
    result = scene.see(folder)
    assert result.checkout is not None and result.project is not None
    proposal = scene.model.open_proposal(
        checkout=result.checkout,
        kind=kind,
        candidates=[result.project],
        evidence={"dir": str(folder.path)},
        reason=kind,
        line="bettermemory: a question",
    )
    before = scene.tables()
    bm(monkeypatch, "project", "folder", proposal.id)
    document = _document(capsys)
    assert document["path"] is None
    assert base64.b64decode(
        document["path_bytes_base64"], validate=True
    ) == os.fsencode(folder.path)
    assert scene.tables() == before
    assert scene.store.log_verify()["status"] == "ok"


async def test_mcp_write_show_and_search_keep_a_native_checkout_origin(
    tmp_path: Path,
) -> None:
    folder = _repository(tmp_path)
    server, store = _server(tmp_path / "store")
    ctx = fake_ctx(headers={identity.HEADER_WORKSPACE_JSON: json.dumps(folder)})
    try:
        written = await _call(
            server,
            "memory_write",
            content="The loader reads its settings once at startup.",
            scopes=["tools"],
            ctx=ctx,
        )
        assert written["status"] == "committed", written
        shown = await _call(server, "memory_show", id=written["id"], ctx=ctx)
        assert shown["origin"]["cwd"] is None
        assert base64.b64decode(shown["origin"]["cwd_bytes_base64"]) == os.fsencode(
            folder
        )
        assert base64.b64decode(
            shown["origin"]["worktree_root_bytes_base64"]
        ) == os.fsencode(folder)
        found = await _call(server, "memory_search", query="loader settings", ctx=ctx)
        assert any(row["id"] == written["id"] for row in found)
        json.dumps(shown, ensure_ascii=False).encode("utf-8")
        json.dumps(found, ensure_ascii=False).encode("utf-8")
        assert store.log_verify()["status"] == "ok"
    finally:
        store.close()
