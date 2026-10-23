"""The write arm of the daemon latency bench (bench/daemon/latency.py).

Unit D1's P10 times memory_write on a scratch copy of the sealed store,
before the cache, with it, and in the review era, and grades the three
files against each other. These tests pin what makes the three comparable
and the arm safe to run: it never opens the live store, each file carries
the version, the commit and the store's pin, the workload is the same 50
new, gate-clean bodies every time, and a review is answered "unrelated,
0, 0" with both calls timed together.

No test reads the sealed copy. The store here is the golden v8 fixture
(`tests/fixtures/v8/store`) packed into a tarball the test writes, with
its `.sha256` beside it, the shape of the real one.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import bettermemory
from bettermemory.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
from bettermemory.search import find_similar
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.v8 import iter_active

_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _ROOT / "bench" / "daemon" / "latency.py"
_FIXTURE = _ROOT / "tests" / "fixtures" / "v8" / "store"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_daemon_latency", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_daemon_latency"] = module
    spec.loader.exec_module(module)
    return module


latency = _load()


def _fixture_tgz(tmp_path: Path) -> Path:
    """The golden v8 store packed as `live-copy/`, like the sealed copy,
    with the sha256 file beside it written in bytes."""
    tgz = tmp_path / "fixture-copy.tgz"
    with tarfile.open(tgz, "w:gz") as archive:
        archive.add(_FIXTURE, arcname="live-copy")
        # The AppleDouble twins macOS tar writes beside every entry, as the
        # sealed copy carries them: one for the directory, one for a file.
        first = sorted(p.name for p in _FIXTURE.glob("*.md"))[0]
        for name in ("._live-copy", f"live-copy/._{first}"):
            junk = b"\x00\x05\x16\x07 not a memory"
            info = tarfile.TarInfo(name)
            info.size = len(junk)
            archive.addfile(info, io.BytesIO(junk))
    digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
    (tmp_path / "fixture-copy.tgz.sha256").write_bytes(
        f"{digest}  fixture-copy.tgz\n".encode("ascii")
    )
    return tgz


def _config(store_dir: Path) -> Config:
    return Config(
        storage=StorageConfig(directory=str(store_dir)),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(),
    )


def test_write_arm_runs_on_a_scratch_copy_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arm refuses the live store, under either home-directory name
    or wherever `$BETTERMEMORY_DIR` points, and a directory inside one,
    before it reads or creates anything; given a scratch directory, the
    store it writes lives there and nowhere else."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    elsewhere = tmp_path / "configured-store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(elsewhere))
    tgz = _fixture_tgz(tmp_path)

    refused = [
        home / ".bettermemory",
        home / ".bettermemory" / "bench",
        home / ".claude-memory",
        elsewhere,
        elsewhere / "nested" / "deeper",
    ]
    for scratch in refused:
        with pytest.raises(SystemExit, match="scratch copy only"):
            latency.run_write_arm(tgz=tgz, scratch=scratch, n=1, warm_ups=0)
        assert not scratch.exists()
    assert not (home / ".bettermemory").exists()
    assert not (home / ".claude-memory").exists()
    assert not elsewhere.exists()

    scratch = tmp_path / "scratch"
    artifact = latency.run_write_arm(tgz=tgz, scratch=scratch, n=2, warm_ups=0)
    assert (scratch / "store" / STORE_FILENAME).is_file()
    assert artifact["results"]["memory_write"]["n"] == 2
    assert not (home / ".bettermemory").exists()
    assert not (home / ".claude-memory").exists()
    assert not elsewhere.exists()
    written = {
        p.relative_to(tmp_path).parts[0] for p in tmp_path.rglob("*") if p.is_file()
    }
    assert written <= {"scratch", "fixture-copy.tgz", "fixture-copy.tgz.sha256"}


def test_write_arm_reports_p50_p95_version_commit_and_store_pin(
    tmp_path: Path,
) -> None:
    """One default run: the file names the package version, the full
    commit the tree was at, the tarball's sha256 (checked against the file
    beside it) and the memories the import found, with p50 and p95 for 50
    single writes and no reviewed_write while no review exists. A tarball
    whose sha256 file disagrees is refused before anything is unpacked."""
    tgz = _fixture_tgz(tmp_path)
    artifact = latency.run_write_arm(tgz=tgz, scratch=tmp_path / "scratch")
    out = tmp_path / "out" / "latency-write.json"
    latency.write_artifact(out, artifact)
    raw = out.read_bytes()
    assert raw.endswith(b"}\n")
    assert b"\r\n" not in raw
    report = json.loads(raw)

    assert report["version"] == bettermemory.__version__
    head = subprocess.run(
        ["git", "-C", str(_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert len(head) == 40
    assert report["commit"] == head
    assert report["provenance"]["commit"] == head

    assert report["store"]["tgz_sha256"] == hashlib.sha256(tgz.read_bytes()).hexdigest()
    assert report["store"]["memories_imported"]["found"] == len(
        list(iter_active(_FIXTURE))
    )

    assert report["workload"]["seed"] == latency.WRITE_SEED
    assert report["workload"]["count"] == latency.WRITE_N == 50
    assert report["workload"]["sha256"] == latency.workload_sha256(
        latency.write_workload(latency.WRITE_SEED, latency.WRITE_N)
    )
    single = report["results"]["memory_write"]
    assert single["n"] == 50
    assert 0 < single["p50_ms"] <= single["p95_ms"] <= single["max_ms"]
    assert "reviewed_write" not in report["results"]
    assert report["outcomes"]["statuses"] == {"committed": 50}
    assert report["outcomes"]["calls_per_write"] == {"1": 50}

    sidecar = tmp_path / "fixture-copy.tgz.sha256"
    sidecar.write_bytes(b"0" * 64 + b"  fixture-copy.tgz\n")
    with pytest.raises(SystemExit, match="records"):
        latency.run_write_arm(tgz=tgz, scratch=tmp_path / "second")
    assert not (tmp_path / "second").exists()
    sidecar.unlink()
    with pytest.raises(SystemExit, match="no fixture-copy.tgz.sha256"):
        latency.run_write_arm(tgz=tgz, scratch=tmp_path / "third")


def test_write_arm_workload_fixed_new_and_gate_clean(tmp_path: Path) -> None:
    """The workload is 50 bodies from the fixed seed, the same on every
    call, distinct from each other by the dedup measure, and on a scratch
    store every body passes the write gates and overlaps nothing there;
    a body that fails any of those stops the check by index."""
    first = latency.write_workload(latency.WRITE_SEED, latency.WRITE_N)
    assert first == latency.write_workload(latency.WRITE_SEED, latency.WRITE_N)
    assert len(first) == 50
    assert len(set(first)) == 50
    assert latency.write_workload(latency.WRITE_SEED, 53)[:50] == first
    assert latency.write_workload(latency.WRITE_SEED + 1, 50) != first
    for index, body in enumerate(first):
        others = [latency._as_memory(b) for j, b in enumerate(first) if j != index]
        assert find_similar(body, others) == []

    v8_root = latency.extract_copy(_fixture_tgz(tmp_path), tmp_path / "copy")
    assert v8_root == tmp_path / "copy" / "live-copy"
    assert not [p for p in (tmp_path / "copy").rglob("._*")]
    store_dir = tmp_path / "store"
    latency.import_copy(v8_root, store_dir)
    store = Store(store_dir)
    config = _config(store_dir)
    try:
        check = latency.check_workload(store, config, first)
        assert check["bodies"] == 50
        assert check["active_in_store"] > 0
        assert check["removed_in_store"] > 0
        assert {
            "CredentialGate",
            "TransientGate",
            "UserClaimGate",
            "ScopeMismatchGate",
            "DedupActiveGate",
            "DedupTombstoneGate",
        } <= set(check["gates"])
        for key in (
            "refused",
            "overlapping_active",
            "overlapping_removed",
            "overlapping_each_other",
        ):
            assert check[key] == 0

        stored = store.load_all()[0].body
        with pytest.raises(SystemExit, match="body 1 overlaps an active memory"):
            latency.check_workload(store, config, [first[0], stored])
        transient = (
            "The release pipeline is currently paused while the signing "
            "service moves hosts, so every build waits in the queue."
        )
        with pytest.raises(SystemExit, match="body 0 refused as transient_warning"):
            latency.check_workload(store, config, [transient])
        with pytest.raises(SystemExit, match="overlaps another workload body"):
            latency.check_workload(store, config, [first[0], first[0]])
    finally:
        store.close()


async def test_write_arm_second_call_answers_unrelated_0_0(tmp_path: Path) -> None:
    """A write answered with a review is re-sent as the same call plus
    every related id "unrelated", unsafe 0 and secret 0, and the review_id;
    the time covers both calls; it lands in reviewed_write, not
    memory_write. A write decided in one call lands in memory_write."""
    now = [0.0]
    sent: list[tuple[str, dict[str, Any]]] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        sent.append((name, json.loads(json.dumps(arguments))))
        if "review" in arguments or arguments["content"] == "decided at once":
            now[0] += 0.007
            return {"status": "committed", "id": "01NEWMEMORY"}
        now[0] += 0.005
        return {
            "status": "review",
            "written": False,
            "review": {
                "review_id": "01REVIEWID",
                "related": [{"id": "01RELATEDA", "text": "a"}, {"id": "01RELATEDB"}],
                "questions": {},
            },
            "hint": "Not written. Answer in `review` and re-send the same call.",
        }

    timings = await latency.time_writes(
        call, ["a reviewed body", "decided at once"], clock=lambda: now[0]
    )

    assert [name for name, _ in sent] == ["memory_write"] * 3
    first, second, third = (arguments for _, arguments in sent)
    assert "review" not in first
    assert second == {
        **first,
        "review": {
            "review_id": "01REVIEWID",
            "relation": {"01RELATEDA": "unrelated", "01RELATEDB": "unrelated"},
            "unsafe": 0,
            "secret": 0,
            "standing": 0,
        },
    }
    assert "review" not in third
    assert timings["reviewed_write"] == [pytest.approx(0.012)]
    assert timings["memory_write"] == [pytest.approx(0.007)]
    assert timings["calls_per_write"] == {"1": 1, "2": 1}
    assert timings["statuses"] == {"committed": 2}

    async def never_decides(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "review", "review": {"review_id": "x", "related": []}}

    with pytest.raises(SystemExit, match="still under review"):
        await latency.time_writes(never_decides, ["body"])

    async def refuses(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "duplicate"}

    with pytest.raises(SystemExit, match="not committed"):
        await latency.time_writes(refuses, ["body"])


def test_write_arm_file_carries_reviewed_write_in_the_review_era(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same arm, unchanged, writes the review-era file: with a server
    that answers every first call with a review, the run records
    results.reviewed_write and no memory_write, and every write commits
    through the real handler on the second call."""
    real = latency.server_call

    def reviewing(server: Any) -> Any:
        inner = real(server)

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if "review" not in arguments:
                return {
                    "status": "review",
                    "written": False,
                    "review": {"review_id": "01REVIEWID", "related": []},
                }
            assert arguments["review"] == {
                "review_id": "01REVIEWID",
                "relation": {},
                "unsafe": 0,
                "secret": 0,
                "standing": 0,
            }
            return await inner(
                name, {k: v for k, v in arguments.items() if k != "review"}
            )

        return call

    monkeypatch.setattr(latency, "server_call", reviewing)
    artifact = latency.run_write_arm(
        tgz=_fixture_tgz(tmp_path), scratch=tmp_path / "scratch", n=3, warm_ups=1
    )
    assert "memory_write" not in artifact["results"]
    assert artifact["results"]["reviewed_write"]["n"] == 3
    assert artifact["outcomes"]["calls_per_write"] == {"2": 3}
