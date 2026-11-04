"""The scale bench's scratch world (ENG-109): the environment every arm
runs in, the repositories its stores' checkouts stand in, the synthetic
clock the generator stamps rows with, the text it writes, and the
helpers each result file shares (percentiles, load averages, a child's
peak RSS, the commit and version stamp).

Nothing here reads or writes the user's store, keys or daemon state.
`scratch_env` points HOME, ``BETTERMEMORY_DIR``, ``BETTERMEMORY_KEYS_DIR``
and ``BETTERMEMORY_STATE_DIR`` at the scratch directory before the
package is imported, refuses a scratch directory inside a live store or
the user's bettermemory directories, and drops the harness's session
variables; `no_network` makes every TCP connect this process attempts
fail and counts it, so an in-process arm that reached for a daemon would
fail loudly rather than reach the user's on 127.0.0.1:7397.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import random
import resource
import secrets
import socket
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
#: The package tree the bench imports: this checkout's, or another tree's
#: `src` named by ``BENCH_SCALE_SRC`` (ENG-159's arm reads the tree before
#: its fix from a `git archive` of it).
SRC = Path(os.environ.get("BENCH_SCALE_SRC") or ROOT / "src").resolve()
BENCH = ROOT / "bench"
for _path in (SRC, BENCH):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

#: The live store's shape, read 2026-11-04 (counts only): what 1x is.
LIVE_SHAPE = {
    "memories": 1_475,
    "episodes": 355,
    "log_rows": 40_789,
    "membership_rows": 3_913,
    "membership_tables": 13,
}

#: The harness's variables a bench process must not carry: the session
#: id would stamp the bench's rows with the session that ran it.
_HARNESS_VARIABLES = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "BETTERMEMORY_WORKSPACE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_INDEX_FILE",
)

#: Below this 1-minute load average a timed arm counts as quiet.
QUIET_LOAD = 4.0


def _user_dirs() -> list[Path]:
    """The directories no scratch directory may be, or sit inside: the
    live store under its names and the user's bettermemory data, state
    and config directories, read before HOME is moved."""
    import platformdirs

    home = Path.home()
    dirs = [home / ".bettermemory", home / ".claude-memory"]
    for reader in (
        platformdirs.user_data_dir,
        platformdirs.user_state_dir,
        platformdirs.user_config_dir,
        platformdirs.user_cache_dir,
    ):
        dirs.append(Path(reader("bettermemory")))
    for name in ("BETTERMEMORY_DIR", "BETTERMEMORY_KEYS_DIR", "BETTERMEMORY_STATE_DIR"):
        value = os.environ.get(name)
        if value:
            dirs.append(Path(value).expanduser())
    return dirs


def refuse_live(path: Path) -> None:
    """SystemExit when `path` is one of the user's bettermemory
    directories or inside one."""
    target = path.expanduser().resolve()
    for live in _user_dirs():
        resolved = live.expanduser().resolve()
        if target == resolved or target.is_relative_to(resolved):
            raise SystemExit(f"refusing {path}: it is or sits inside {live}")


def scratch_env(scratch: Path) -> dict[str, str]:
    """Point this process (and every child) at `scratch`, and return the
    environment. Call before importing the package."""
    scratch = scratch.resolve()
    refuse_live(scratch)
    for name in ("home", "store", "state", "keys", "repos", "transcripts"):
        (scratch / name).mkdir(parents=True, exist_ok=True)
    gitconfig = scratch / "gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text(
            "[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n"
            "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
    for name in _HARNESS_VARIABLES:
        os.environ.pop(name, None)
    os.environ.update(
        {
            "HOME": str(scratch / "home"),
            "BETTERMEMORY_DIR": str(scratch / "store"),
            "BETTERMEMORY_KEYS_DIR": str(scratch / "keys"),
            "BETTERMEMORY_STATE_DIR": str(scratch / "state"),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CEILING_DIRECTORIES": str(scratch),
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "PYTHONPATH": str(SRC),
        }
    )
    return dict(os.environ)


class _Refused(OSError):
    pass


@contextlib.contextmanager
def no_network() -> Iterator[list[str]]:
    """Every TCP connect made in this process inside the block fails, and
    each one is listed in the yielded list: the in-process arms make
    none, so a non-empty list means an arm reached for a daemon."""
    attempts: list[str] = []
    real = socket.socket.connect
    real_ex = socket.socket.connect_ex

    def refused(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(repr(address))
            raise _Refused(f"the scale bench makes no TCP connection ({address!r})")
        return real(self, address)

    def refused_ex(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(repr(address))
            return 111
        return real_ex(self, address)

    socket.socket.connect = refused  # type: ignore[method-assign]
    socket.socket.connect_ex = refused_ex  # type: ignore[method-assign]
    try:
        yield attempts
    finally:
        socket.socket.connect = real  # type: ignore[method-assign]
        socket.socket.connect_ex = real_ex  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} in {cwd}: {done.stderr.strip()}")
    return done.stdout.strip()


def make_repo(
    path: Path, *, remote: str | None, commit: bool = True, files: int = 3
) -> Path:
    """A repository at `path` with one commit of a few files of its own
    (none when `commit` is false), `remote` as ``origin``. Returns its
    root as git names it."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    if commit:
        token = secrets.token_hex(6)
        for index in range(files):
            (path / f"module_{index}.py").write_text(
                f"# {path.name} {index} {token}\n", encoding="utf-8"
            )
        (path / "src").mkdir(exist_ok=True)
        (path / "src" / "main.py").write_text(f"# {token}\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"{path.name} {token}")
    if remote is not None:
        git(path, "remote", "add", "origin", remote)
    return Path(git(path, "rev-parse", "--show-toplevel"))


def add_worktree(repo: Path, path: Path, branch: str) -> Path:
    git(repo, "worktree", "add", "-q", "-b", branch, str(path))
    return Path(git(path, "rev-parse", "--show-toplevel"))


def clone(source: Path, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    remote = git(source, "remote", "get-url", "origin")
    git(path.parent, "clone", "-q", str(source), str(path))
    git(path, "remote", "set-url", "origin", remote)
    return Path(git(path, "rev-parse", "--show-toplevel"))


# ---------------------------------------------------------------------------
# The synthetic clock and ids
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _crockford(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def ulid_at(when: datetime, rng: random.Random) -> str:
    """A ULID minted at `when`, so ids sort by the synthetic time as the
    product's own sort by the wall clock."""
    ms = int(when.timestamp() * 1000) & ((1 << 48) - 1)
    return _crockford(ms, 10) + _crockford(rng.getrandbits(80), 16)


def iso(when: datetime) -> str:
    from bettermemory.time_utils import isoformat_utc

    return isoformat_utc(when)


@dataclass
class Clock:
    """The generator's clock: every log row the store appends while
    `stamping` holds is stamped `now`, which the generator moves forward."""

    now: datetime

    def iso(self) -> str:
        return iso(self.now)


@contextlib.contextmanager
def stamping(clock: Clock) -> Iterator[None]:
    """Stamp the log rows the store appends, and the verification stamp,
    with the synthetic clock: `log._utcnow_iso` is the one stamp
    `append_row` takes when its caller names none, and `store_records`
    reads `utcnow` for `mark_verified`. Every other field the generator
    writes names its time itself."""
    from bettermemory import log as _log
    from bettermemory import store_records as _records

    real_iso, real_now = _log._utcnow_iso, _records.utcnow
    _log._utcnow_iso = clock.iso
    _records.utcnow = lambda: clock.now
    try:
        yield
    finally:
        _log._utcnow_iso = real_iso
        _records.utcnow = real_now


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def days(value: float) -> timedelta:
    return timedelta(days=value)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

_SHARED = (
    "deploy service config cache index queue worker schema migration "
    "release branch build test fixture coverage lint format type check "
    "daemon hook session prompt brief search write review store log key "
    "backup restore verify chain token checkout project remote commit "
    "timeout retry budget latency memory window lock transaction batch"
).split()

_ROOTS = (
    "amber basalt cedar delta ember fjord garnet harbor indigo juniper "
    "kestrel lumen meridian nimbus onyx prairie quartz raven sierra tundra "
    "umber vesper willow xenon yarrow zephyr alder birch cobalt dune "
    "elm flint glacier heron iris jade kelp larch maple nectar"
).split()

_VERBS = (
    "uses keeps reads writes runs sets moves holds checks names takes "
    "builds sends opens closes signs pins drops waits serves"
).split()


def project_words(index: int, rng: random.Random) -> list[str]:
    """The words a project's records and prompts draw on: a few of its
    own made-up nouns beside the shared vocabulary."""
    root = _ROOTS[index % len(_ROOTS)]
    own = [f"{root}{suffix}" for suffix in ("", "d", "ctl", "net", "db", "api")]
    rng.shuffle(own)
    return own + rng.sample(_SHARED, 18)


def sentence(rng: random.Random, words: Sequence[str], n: int) -> str:
    picked = [rng.choice(words) for _ in range(n)]
    picked.insert(1, rng.choice(_VERBS))
    return " ".join(picked).capitalize() + "."


_SYLLABLES = (
    "ba be bi bo bu da de di do du ka ke ki ko ku la le li lo lu ma me mi mo "
    "mu na ne ni no nu ra re ri ro ru sa se si so su ta te ti to tu va ve vi "
    "vo vu za ze zi zo zu"
).split()


def noun(rng: random.Random) -> str:
    """A made-up noun of two to four syllables: what keeps one record's
    words its own, as names of files, flags and hosts do."""
    return "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 4)))


def body(rng: random.Random, words: Sequence[str]) -> str:
    """A record body of two to five sentences, about 150 to 600
    characters, over the project's words and a few made-up nouns of its
    own, with a number in it as durable facts carry."""
    own = [noun(rng) for _ in range(rng.randint(3, 6))]
    pool = list(words) + own * 3
    parts = [sentence(rng, pool, rng.randint(8, 18)) for _ in range(rng.randint(2, 5))]
    parts.insert(
        rng.randint(0, len(parts)),
        f"The {own[0]} limit is {rng.randint(2, 900)} in {own[-1]}.",
    )
    return " ".join(parts)


def prompt(rng: random.Random, words: Sequence[str]) -> str:
    """An owner prompt: a question or an instruction over the project's
    words, 40 to 400 characters."""
    lead = rng.choice(("How does", "Why does", "Fix the", "Can you check", "Update"))
    text = " ".join(rng.choice(words) for _ in range(rng.randint(5, 40)))
    return f"{lead} {text}?"


def query(rng: random.Random, words: Sequence[str]) -> str:
    return " ".join(rng.sample(list(words), rng.randint(2, 4)))


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def pct(values: Sequence[float], q: float) -> float:
    """The `q`-th percentile by nearest rank on the sorted values (the
    daemon latency bench's rule)."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((q / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def summary(seconds: Sequence[float]) -> dict[str, Any]:
    if not seconds:
        return {"n": 0}
    ms = [v * 1000.0 for v in seconds]
    return {
        "n": len(ms),
        "p50_ms": round(statistics.median(ms), 2),
        "p95_ms": round(pct(ms, 95), 2),
        "p99_ms": round(pct(ms, 99), 2),
        "mean_ms": round(statistics.fmean(ms), 2),
        "min_ms": round(min(ms), 2),
        "max_ms": round(max(ms), 2),
    }


def loads() -> list[float]:
    return [round(v, 2) for v in os.getloadavg()]


def wait_quiet(
    *, threshold: float = QUIET_LOAD, max_wait: float = 90 * 60, poll: float = 180
) -> dict[str, Any]:
    """Wait until the 1-minute load average is under `threshold`, polling
    every `poll` seconds, for at most `max_wait` seconds. Returns whether
    it was quiet, how long it waited, and the loads it saw."""
    started = time.monotonic()
    seen = [loads()]
    while seen[-1][0] >= threshold and time.monotonic() - started < max_wait:
        print(
            f"load {seen[-1]} >= {threshold}; waiting {poll:.0f} s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(poll)
        seen.append(loads())
    return {
        "quiet_at_start": seen[-1][0] < threshold,
        "waited_s": round(time.monotonic() - started, 1),
        "polls": len(seen),
        "loads_at_start": seen[-1],
    }


@contextlib.contextmanager
def timed_arm(record: dict[str, Any], name: str) -> Iterator[None]:
    """Record the load averages at the start and end of an arm, and its
    wall time, under `record[name]`."""
    entry = record.setdefault(name, {})
    entry["load_start"] = loads()
    started = time.perf_counter()
    try:
        yield
    finally:
        entry["load_end"] = loads()
        entry["wall_s"] = round(time.perf_counter() - started, 2)


#: The most RSS a measured child may reach before it is stopped, in MB:
#: the bench shares its machine, and a whole-store command whose memory
#: grows with the store would otherwise take what the others need.
RSS_CAP_MB = float(os.environ.get("BENCH_SCALE_RSS_CAP_MB", "9000"))


def available_mb() -> float | None:
    """The memory the machine could give a new process without paging,
    in MB: free, inactive, speculative and purgeable pages on macOS
    (`vm_stat`), MemAvailable on Linux."""
    if sys.platform == "darwin":
        text = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
        size = 16384
        pages = 0
        for line in text.splitlines():
            if "page size of" in line:
                size = int(line.split("page size of")[1].split()[0])
            name, _, value = line.partition(":")
            if name.strip() in (
                "Pages free",
                "Pages inactive",
                "Pages speculative",
                "Pages purgeable",
            ):
                pages += int(value.strip().rstrip("."))
        return round(pages * size / 1e6, 1)
    with contextlib.suppress(OSError, ValueError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return round(int(line.split()[1]) * 1024 / 1e6, 1)
    return None


def _rss_mb(pid: int) -> float | None:
    done = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True
    )
    text = done.stdout.strip()
    return int(text) * 1024 / 1e6 if text.isdigit() else None


def run_measured(
    argv: Sequence[str],
    env: dict[str, str],
    *,
    cwd: Path | None = None,
    cap_mb: float | None = None,
) -> dict[str, Any]:
    """Run `argv` to its end and return its exit code, wall time, peak RSS
    in MB (the child's own `ru_maxrss`, read by `os.wait4` on its pid) and
    the tail of its output. Output goes to files so the child never
    blocks on a full pipe while this process waits on it. A watcher reads
    the child's RSS four times a second, with the time it first passed
    each GB, and stops the child once it passes `cap_mb` (`RSS_CAP_MB`
    when None; `capped`)."""
    import tempfile
    import threading

    cap = RSS_CAP_MB if cap_mb is None else cap_mb
    watch: dict[str, Any] = {"capped": False, "gb_at_s": {}}
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        started = time.perf_counter()
        proc = subprocess.Popen(list(argv), env=env, cwd=cwd, stdout=out, stderr=err)
        done = threading.Event()

        def watcher() -> None:
            while not done.wait(0.25):
                rss = _rss_mb(proc.pid)
                if rss is None:
                    continue
                gb = int(rss // 1000)
                if gb and str(gb) not in watch["gb_at_s"]:
                    watch["gb_at_s"][str(gb)] = round(time.perf_counter() - started, 1)
                if rss > cap:
                    watch["capped"] = True
                    with contextlib.suppress(OSError):
                        proc.kill()
                    return

        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        _, status, usage = os.wait4(proc.pid, 0)
        done.set()
        thread.join()
        wall = time.perf_counter() - started
        proc.returncode = os.waitstatus_to_exitcode(status)
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(), err.read()
    unit = 1 if sys.platform == "darwin" else 1024
    return {
        "exit": proc.returncode,
        "wall_s": round(wall, 2),
        "peak_rss_mb": round(usage.ru_maxrss * unit / 1e6, 1),
        "rss_cap_mb": cap,
        "capped": watch["capped"],
        "gb_at_s": watch["gb_at_s"],
        "stdout": stdout,
        "stderr_tail": stderr[-600:],
    }


def self_peak_rss_mb() -> float:
    unit = 1 if sys.platform == "darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit / 1e6, 1)


def machine() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
    }
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
            raw = subprocess.run(
                ["sysctl", "-n", "hw.memsize", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            info["ram_gb"] = round(int(raw[0]) / 2**30, 1)
            info["cpu"] = raw[1].strip()
    return info


def stamp() -> dict[str, Any]:
    """The code a result measured: the checkout's HEAD, whether its
    tracked files differ from it, and the tree's declared version."""

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()

    version = "unknown"
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            version = line.split("=", 1)[1].strip().strip('"')
            break
    import hashlib

    program = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        program.update(path.name.encode() + b"\0" + path.read_bytes())
    return {
        "commit": run("rev-parse", "HEAD"),
        "src_dirty": bool(
            run("status", "--porcelain", "--untracked-files=no", "--", "src")
        ),
        "version": version,
        "src": str(SRC.relative_to(ROOT))
        if SRC.is_relative_to(ROOT)
        else "BENCH_SCALE_SRC",
        "program_sha256": program.hexdigest(),
        "machine": machine(),
        "at": iso(datetime.now(timezone.utc)),
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a result once: a reading is a new file, never a rewrite."""
    if path.exists():
        raise SystemExit(f"{path} exists; a new reading is a new file")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
