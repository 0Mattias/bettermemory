"""The previous published release against this checkout, across a candidate
boundary (ENG-107).

tests/test_cross_build.py and tests/test_shim_versions.py stand the other
build in with this build's own code, patched to report another revision or
version. Here both builds are real installs, each in a venv of its own: the
previous release as PyPI serves it, and this checkout, built as the
candidate after that release when the two carry one version. CI's
cross-release job makes the two venvs (tools/ci/previous_release.py,
tools/ci/cross_release_builds.py) and names their ``bettermemory``
executables in ``BETTERMEMORY_CI_PREVIOUS`` and ``BETTERMEMORY_CI_NEXT``;
without both the module is skipped.

Only what a user's install meets is driven: the CLI, and the MCP tools
through ``serve`` (the server in process) or through the stdio shim in
front of a daemon, spoken to as JSON-RPC lines. Neither build is imported
here, and the store file is read only with sqlite3, read-only, for meta's
version keys.

A store crosses both ways: one the previous release made is opened by this
checkout (whose writing open runs the schema steps) and then by the
previous release again, and one this checkout made is opened by the
previous release. The previous release's write must commit, or be refused
in words that say to upgrade exactly when meta's ``min_writer_revision`` is
above the revision the previous release writes (meta's ``schema_revision``
in a store it made). A daemon crosses both ways: the previous release's
shim, ``status`` and session-start hook use this checkout's daemon and leave
it running, and this checkout's ``up`` replaces the previous release's
daemon under an open shim of the previous release, which follows it and
asks for a restart of its MCP client.

Every child runs in a sandbox under the test's own directory: an
environment built from an allowlist (`Pair.env`), never a copy of this
process's, with HOME, the XDG directories, TMPDIR, the store, keys and
state directories all inside it, the working directory inside it, and a
search path with no other install's ``bettermemory`` on it. Before any
daemon command each build is asked where it resolves its config, store,
keys and state (``config path --json``) and the test fails unless every
one is inside the sandbox (`Pair.check_isolation`); the daemon port is 0
(ephemeral) in the sandbox's config. Every daemon a test starts is stopped
in a finally, and every wait is bounded.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PREVIOUS_ENV = "BETTERMEMORY_CI_PREVIOUS"
NEXT_ENV = "BETTERMEMORY_CI_NEXT"

pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get(PREVIOUS_ENV) and os.environ.get(NEXT_ENV)),
        reason=f"needs {PREVIOUS_ENV} and {NEXT_ENV} (the cross-release job)",
    ),
    pytest.mark.skipif(os.name == "nt", reason="the cross-release job runs on POSIX"),
]

#: The bound on one CLI command, on one MCP request and on a daemon's change.
CLI_SECONDS = 120.0
MCP_SECONDS = 60.0
DAEMON_SECONDS = 30.0
#: SIGKILL where the platform has it: the job runs on Linux, and Windows
#: has no SIGKILL for its type check to find.
_SIGKILL: int = getattr(signal, "SIGKILL", 9)
#: Variables carried from this process's environment, when set. Everything
#: else a child sees is set by `Pair.env`.
CARRIED = ("LANG", "LC_ALL", "LC_CTYPE")
#: The variables that decide where a build keeps its files, each set inside
#: the sandbox.
PATH_VARIABLES = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "TMPDIR",
    "BETTERMEMORY_DIR",
    "BETTERMEMORY_KEYS_DIR",
    "BETTERMEMORY_STATE_DIR",
)
#: A shim's notice that its daemon changed version under an open session
#: (`_daemon_client._session_version`).
RESTART = "restart the MCP client"
PROTOCOL_VERSION = "2025-06-18"


# ---------------------------------------------------------------------------
# The two builds and their sandbox
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Build:
    """One install: its name here and its ``bettermemory`` executable."""

    name: str
    exe: Path

    @property
    def bin_dir(self) -> Path:
        return self.exe.parent

    @property
    def venv(self) -> Path:
        return self.exe.parent.parent


def _builds() -> tuple[Build, Build]:
    return (
        Build("previous", Path(os.environ.get(PREVIOUS_ENV, "")).absolute()),
        Build("next", Path(os.environ.get(NEXT_ENV, "")).absolute()),
    )


def _search_path(build: Build) -> str:
    """The build's own bin directory, then this process's PATH without any
    directory that holds another ``bettermemory``."""
    kept = [str(build.bin_dir)]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry) or entry in kept:
            continue
        if os.path.lexists(os.path.join(entry, "bettermemory")):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def _under(path: str | Path, root: Path) -> bool:
    return Path(path).expanduser().resolve().is_relative_to(root.resolve())


class Pair:
    """The previous release and this checkout, sharing one sandbox: one
    store, keys and state directory, used by both in turn."""

    def __init__(self, root: Path, previous: Build, after: Build) -> None:
        self.root = root.resolve()
        self.previous = previous
        self.next = after
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.store_dir = self.root / "store"
        self.keys_dir = self.root / "keys"
        self.state_dir = self.root / "state"
        self.backups = self.root / "backups"
        self.logs = self.root / "logs"
        for directory in (self.home, self.work, self.root / "tmp", self.logs):
            directory.mkdir(parents=True, exist_ok=True)
        self.versions: dict[str, str] = {}
        self._isolated: set[str] = set()

    @property
    def store_file(self) -> Path:
        return self.store_dir / "memory.sqlite"

    def env(self, build: Build, *, store_dir: Path | None = None) -> dict[str, str]:
        """The whole environment of `build`'s children: the allowlist."""
        env = {name: os.environ[name] for name in CARRIED if name in os.environ}
        env.update(
            {
                "PATH": _search_path(build),
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                "XDG_DATA_HOME": str(self.home / ".local" / "share"),
                "XDG_CACHE_HOME": str(self.home / ".cache"),
                "TMPDIR": str(self.root / "tmp"),
                "BETTERMEMORY_DIR": str(store_dir or self.store_dir),
                "BETTERMEMORY_KEYS_DIR": str(self.keys_dir),
                "BETTERMEMORY_STATE_DIR": str(self.state_dir),
                "GIT_CEILING_DIRECTORIES": str(self.root),
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return env

    def cli(
        self,
        build: Build,
        *args: str,
        stdin: str = "",
        store_dir: Path | None = None,
        timeout: float = CLI_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(build.exe), *args],
            env=self.env(build, store_dir=store_dir),
            cwd=self.work,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def check_isolation(self, build: Build) -> None:
        """Fail unless every path `build` would use is inside the sandbox:
        the environment's path variables, and the config file, store, keys
        and state directories the build itself resolves. Writes the
        sandbox's config (the daemon port 0, ephemeral) and reads it back.
        Every daemon command (`daemon_cli`), shim and server (`serve`)
        requires this for its build first."""
        env = self.env(build)
        outside = [name for name in PATH_VARIABLES if not _under(env[name], self.root)]
        assert not outside, f"outside the sandbox: {outside}"
        assert set(env) <= {
            *CARRIED,
            *PATH_VARIABLES,
            "PATH",
            "GIT_CEILING_DIRECTORIES",
            "GIT_CONFIG_NOSYSTEM",
            "PYTHONDONTWRITEBYTECODE",
        }
        version = self.cli(build, "--version")
        assert version.returncode == 0, version.stderr
        self.versions[build.name] = version.stdout.split()[-1]
        found = self.cli(build, "config", "path", "--json")
        assert found.returncode == 0, found.stderr
        paths = json.loads(found.stdout)
        resolved = {
            key: paths[key]["path"] for key in ("config", "store", "keys", "state")
        }
        outside = [
            f"{key} {path}"
            for key, path in resolved.items()
            if not _under(path, self.root)
        ]
        assert not outside, f"{build.name} resolves outside the sandbox: {outside}"
        assert _under(resolved["state"], self.state_dir), resolved
        assert _under(resolved["store"], self.store_dir), resolved
        config = Path(resolved["config"])
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[daemon]\nport = 0\n", encoding="utf-8")
        shown = self.cli(build, "config", "show", "--json")
        assert shown.returncode == 0, shown.stderr
        port = json.loads(shown.stdout)["settings"]["daemon"]["port"]["value"]
        assert port == 0, f"{build.name} reads the daemon port {port}"
        self._isolated.add(build.name)

    def daemon_cli(
        self, build: Build, *args: str, stdin: str = ""
    ) -> subprocess.CompletedProcess[str]:
        assert build.name in self._isolated, f"{build.name}'s isolation is unchecked"
        return self.cli(build, *args, stdin=stdin)

    def serve(self, build: Build, *, store_dir: Path | None = None) -> McpSession:
        """`build`'s MCP server in process, over stdio."""
        assert build.name in self._isolated, f"{build.name}'s isolation is unchecked"
        return McpSession(
            [str(build.exe), "serve"],
            env=self.env(build, store_dir=store_dir),
            cwd=self.work,
            stderr=self._log(build, "serve"),
        )

    def shim(self, build: Build) -> McpSession:
        """`build`'s stdio shim, which starts or finds the daemon."""
        assert build.name in self._isolated, f"{build.name}'s isolation is unchecked"
        return McpSession(
            [str(build.exe)],
            env=self.env(build),
            cwd=self.work,
            stderr=self._log(build, "shim"),
        )

    def _log(self, build: Build, what: str) -> Path:
        count = len(list(self.logs.glob(f"{build.name}-{what}-*.stderr")))
        return self.logs / f"{build.name}-{what}-{count}.stderr"

    def daemon_state(self) -> dict[str, Any] | None:
        """The one daemon state file in the sandbox, or None."""
        files = sorted(self.state_dir.glob("daemon-*.json"))
        assert len(files) <= 1, files
        if not files:
            return None
        try:
            state: dict[str, Any] = json.loads(files[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return state

    def wait_daemon(
        self, version: str, *, other_than: int | None = None
    ) -> dict[str, Any]:
        """The daemon state once a daemon of `version` (with a pid other
        than `other_than`) answers `status --check` as running."""
        deadline = time.monotonic() + DAEMON_SECONDS
        while True:
            state = self.daemon_state()
            if (
                state is not None
                and state.get("version") == version
                and state.get("pid") != other_than
                and self.daemon_cli(self.next, "status", "--check").returncode == 0
            ):
                return state
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"no daemon of {version} answered; state {_public(state)}"
                )
            time.sleep(0.2)

    def stop_daemons(self) -> None:
        """`down` with each build, then end what still runs from a state
        file in the sandbox and was started from one of the two venvs."""
        for build in (self.next, self.previous):
            if build.name in self._isolated:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.cli(build, "down", timeout=DAEMON_SECONDS * 2)
        for state_file in self.state_dir.glob("daemon-*.json"):
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                _end(int(json.loads(state_file.read_text())["pid"]), self)

    def venvs(self) -> tuple[Path, Path]:
        return self.previous.venv, self.next.venv


def _command_line(pid: int) -> str:
    found = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return found.stdout.strip()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _end(pid: int, pair: Pair) -> None:
    """SIGTERM, then SIGKILL, a daemon this test started: one whose command
    line runs a Python of one of the pair's venvs. Any other pid is left."""
    command = _command_line(pid)
    if not any(str(venv) in command for venv in pair.venvs()):
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + DAEMON_SECONDS
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _alive(pid):
        os.kill(pid, _SIGKILL)


# ---------------------------------------------------------------------------
# MCP over stdio, as JSON-RPC lines
# ---------------------------------------------------------------------------


class RpcError(Exception):
    """A JSON-RPC error reply."""

    def __init__(self, method: str, error: Mapping[str, Any]) -> None:
        super().__init__(f"{method}: {error.get('code')} {error.get('message')}")
        self.error = dict(error)


@dataclass(frozen=True)
class Reply:
    """A tool's result: whether it is an error, its text, and its value
    (the structured content, or the text read as JSON)."""

    error: bool
    text: str
    value: Any

    @classmethod
    def of(cls, result: Mapping[str, Any]) -> Reply:
        text = "".join(
            str(block.get("text", "")) for block in result.get("content") or ()
        )
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and set(structured) == {"result"}:
            value: Any = structured["result"]
        elif structured is not None:
            value = structured
        else:
            try:
                value = json.loads(text)
            except ValueError:
                value = None
        return cls(bool(result.get("isError")), text, value)


class McpSession:
    """One MCP server process over stdio. Every read is bounded."""

    def __init__(
        self, argv: list[str], *, env: dict[str, str], cwd: Path, stderr: Path
    ) -> None:
        self.stderr_path = stderr
        self._stderr = stderr.open("wb")
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=env,
            cwd=cwd,
        )
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._last_id = 0

    def __enter__(self) -> McpSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def stderr(self) -> str:
        """What the server has written to stderr so far."""
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")

    def _send(self, message: Mapping[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    def request(
        self, method: str, params: Mapping[str, Any], *, timeout: float = MCP_SECONDS
    ) -> dict[str, Any]:
        self._last_id += 1
        ident = self._last_id
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"no reply to {method} within {timeout:g} s; stderr:\n"
                    + self.stderr()[-4000:]
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(
                    f"the server exited ({self.proc.poll()}) before it answered "
                    f"{method}; stderr:\n" + self.stderr()[-4000:]
                )
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message:
                if "id" in message:
                    self._answer(message)
                continue
            if message.get("id") != ident:
                continue
            if "error" in message:
                raise RpcError(method, message["error"])
            result: dict[str, Any] = message["result"]
            return result

    def _answer(self, message: Mapping[str, Any]) -> None:
        """Answer a request of the server's: a ping, and nothing else."""
        if message["method"] == "ping":
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "not offered by this client"},
            }
        )

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "cross-release-test", "version": "1"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def tool_names(self) -> set[str]:
        listed = self.request("tools/list", {})
        return {str(tool["name"]) for tool in listed["tools"]}

    def call(self, name: str, arguments: Mapping[str, Any]) -> Reply:
        return Reply.of(
            self.request("tools/call", {"name": name, "arguments": dict(arguments)})
        )

    def close(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=20)
        self._stderr.close()


# ---------------------------------------------------------------------------
# The tools and commands, read
# ---------------------------------------------------------------------------


def write(session: McpSession, content: str, scopes: list[str]) -> Reply:
    """`memory_write`, its review answered: every related statement read as
    unrelated (each fixture statement is about a subject of its own), every
    score 0, and no roster record named. The reply that is not a review."""
    arguments: dict[str, Any] = {"content": content, "scopes": scopes}
    answers: dict[str, Any] = {}
    asked: list[str] = []
    for _ in range(4):
        reply = session.call(
            "memory_write", {**arguments, "review": answers} if answers else arguments
        )
        value = reply.value
        if (
            reply.error
            or not isinstance(value, dict)
            or value.get("status") != "review"
        ):
            return reply
        asked.append(reply.text)
        review = value["review"]
        answers["review_id"] = review["review_id"]
        if "related" in review:
            answers["relation"] = {
                str(entry["id"]): "unrelated" for entry in review["related"]
            }
            answers.update(unsafe=0.0, secret=0.0, standing=0.0)
        if "roster" in review:
            answers["named"] = []
    raise AssertionError(f"the review of {content!r} did not end: {asked[-1]}")


def committed(reply: Reply) -> str:
    """The id of a committed write."""
    assert not reply.error, reply.text
    assert isinstance(reply.value, dict), reply.text
    assert reply.value.get("status") == "committed", reply.text
    return str(reply.value["id"])


def hit_ids(reply: Reply) -> set[str]:
    assert not reply.error, reply.text
    assert isinstance(reply.value, list), reply.text
    return {
        str(hit["id"]) for hit in reply.value if isinstance(hit, dict) and "id" in hit
    }


def found(session: McpSession, query: str) -> set[str]:
    return hit_ids(session.call("memory_search", {"query": query, "max_results": 20}))


def write_all(
    session: McpSession, memories: Mapping[str, tuple[str, str]]
) -> dict[str, str]:
    """Write each memory, keyed by a word only it holds: {word: id}."""
    return {
        word: committed(write(session, text, [scope]))
        for word, (text, scope) in memories.items()
    }


def find_all(session: McpSession, written: Mapping[str, str]) -> None:
    """Each memory is found by the word only it holds."""
    for word, memory_id in written.items():
        assert memory_id in found(session, word), (word, memory_id)


def meta(store_file: Path) -> dict[str, str]:
    """The store's meta table, read with sqlite3, read-only."""
    uri = store_file.resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return {str(key): str(value) for key, value in rows}


def writer_minimum(values: Mapping[str, str]) -> int:
    return int(values.get("min_writer_revision", "0"))


def expect_write(reply: Reply, store_meta: Mapping[str, str], revision: int) -> None:
    """A write of the build that writes `revision`, on a store with
    `store_meta`: committed, or refused saying to upgrade exactly when the
    store needs a later writer."""
    if writer_minimum(store_meta) > revision:
        status = reply.value.get("status") if isinstance(reply.value, dict) else None
        assert reply.error or status != "committed", reply.text
        assert "upgrade" in reply.text.lower(), reply.text
    else:
        committed(reply)


def ok(result: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
    assert result.returncode == 0, (
        result.args,
        result.stdout[-4000:],
        result.stderr[-4000:],
    )
    return result


def log_verify(pair: Pair, build: Build) -> None:
    ok(pair.cli(build, "log", "verify"))


def backup_create(pair: Pair, build: Build) -> str:
    out = pair.backups / build.name
    created = ok(pair.cli(build, "backup", "create", "--out", str(out), "--json"))
    return str(json.loads(created.stdout)["bundle"])


def backup_verify(pair: Pair, build: Build, bundle: str) -> None:
    ok(pair.cli(build, "backup", "verify", bundle))


def previous_revision(pair: Pair) -> int:
    """The schema revision the previous release writes: meta's
    ``schema_revision`` of a store it makes, in a directory of its own."""
    probe = pair.root / "probe-store"
    with pair.serve(pair.previous, store_dir=probe) as session:
        session.initialize()
    return int(meta(probe / "memory.sqlite")["schema_revision"])


def _public(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """A state file without its token, for a message."""
    return {k: v for k, v in (state or {}).items() if k != "token"}


def _once_more(session: McpSession, name: str, arguments: Mapping[str, Any]) -> Reply:
    """`name` called, and called once more when the first call failed."""
    try:
        reply = session.call(name, arguments)
        if not reply.error:
            return reply
        first = reply.text
    except RpcError as exc:
        first = str(exc)
    reply = session.call(name, arguments)
    assert not reply.error, (first, reply.text)
    return reply


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[Pair]:
    previous, after = _builds()
    made = Pair(tmp_path / "sandbox", previous, after)
    try:
        made.check_isolation(previous)
        made.check_isolation(after)
        assert made.versions["previous"] != made.versions["next"], (
            f"both builds are bettermemory {made.versions['next']}: nothing crosses"
        )
        yield made
    finally:
        made.stop_daemons()


#: The fixture statements, each keyed by a word only it holds, with its scope.
PREVIOUS_FIRST = {
    "ruff": (
        "The cross-release fixture project formats its Python with ruff.",
        "tools",
    ),
    "pytest": (
        "The cross-release fixture project runs its suite with pytest.",
        "testing",
    ),
    "lockfile": (
        "The cross-release fixture project pins dependencies in a lockfile.",
        "build",
    ),
}
NEXT_THEN = {
    "pyright": ("The cross-release fixture project type-checks with pyright.", "tools"),
    "coverage": (
        "The cross-release fixture project measures branch coverage.",
        "testing",
    ),
}
PREVIOUS_AGAIN = (
    "The cross-release fixture project builds its docs with mkdocs.",
    "docs",
)
NEXT_FIRST = {
    "shellcheck": (
        "The second cross-release fixture lints shell with shellcheck.",
        "tools",
    ),
    "tagged": (
        "The second cross-release fixture deploys from tagged commits.",
        "release",
    ),
}
PREVIOUS_THEN = (
    "The second cross-release fixture publishes wheels to an index.",
    "release",
)


def test_a_store_of_the_previous_release_crosses_to_this_checkout_and_back(
    pair: Pair,
) -> None:
    old, new = pair.previous, pair.next
    # a. The previous release makes the store.
    with pair.serve(old) as session:
        assert session.initialize()["serverInfo"]["version"] == pair.versions[old.name]
        first = write_all(session, PREVIOUS_FIRST)
    log_verify(pair, old)
    old_bundle = backup_create(pair, old)
    made = meta(pair.store_file)
    revision = int(made["schema_revision"])
    assert writer_minimum(made) <= revision, made

    # b. This checkout opens it, and its writing open runs the schema steps.
    with pair.serve(new) as session:
        assert session.initialize()["serverInfo"]["version"] == pair.versions[new.name]
        find_all(session, first)
        then = write_all(session, NEXT_THEN)
    log_verify(pair, new)
    backup_verify(pair, new, old_bundle)
    new_bundle = backup_create(pair, new)
    opened = meta(pair.store_file)

    # c. The previous release again: it finds both builds' memories, and its
    # write is held to the writer minimum this checkout left in meta.
    with pair.serve(old) as session:
        session.initialize()
        find_all(session, {**first, **then})
        text, scope = PREVIOUS_AGAIN
        expect_write(write(session, text, [scope]), opened, revision)
    log_verify(pair, old)
    backup_verify(pair, old, new_bundle)
    log_verify(pair, new)


def test_a_store_of_this_checkout_is_read_and_written_by_the_previous_release(
    pair: Pair,
) -> None:
    old, new = pair.previous, pair.next
    revision = previous_revision(pair)
    # d. This checkout makes the store; the previous release reads, writes
    # and verifies it.
    with pair.serve(new) as session:
        session.initialize()
        first = write_all(session, NEXT_FIRST)
    log_verify(pair, new)
    made = meta(pair.store_file)
    with pair.serve(old) as session:
        session.initialize()
        find_all(session, first)
        text, scope = PREVIOUS_THEN
        expect_write(write(session, text, [scope]), made, revision)
    log_verify(pair, old)
    log_verify(pair, new)


def test_the_previous_releases_shim_and_hook_use_this_checkouts_daemon(
    pair: Pair,
) -> None:
    old, new = pair.previous, pair.next
    # e. This checkout's daemon up, then the previous release's clients: each
    # keeps the newer daemon rather than replacing it.
    ok(pair.daemon_cli(new, "up", "--port", "0"))
    serving = pair.wait_daemon(pair.versions[new.name])
    serving_key = (serving["pid"], serving["version"])
    with pair.shim(old) as session:
        init = session.initialize()
        assert init["serverInfo"]["version"] == pair.versions[old.name]
        assert {"memory_write", "memory_search"} <= session.tool_names()
        memory_id = committed(
            write(session, "The cross-release fixture caches wheels in CI.", ["build"])
        )
        assert memory_id in found(session, "wheels")
    shim_said = session.stderr()
    after = pair.daemon_state()
    assert after is not None, _public(serving)
    assert (after["pid"], after["version"]) == serving_key, _public(after)
    assert RESTART not in shim_said, shim_said

    status = ok(pair.daemon_cli(old, "status", "--json"))
    reported = json.loads(status.stdout)
    assert reported["state"] == "running", reported
    assert (reported["pid"], reported["version"]) == serving_key, reported

    payload = {
        "session_id": "cross-release-session",
        "cwd": str(pair.work),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    hook = pair.daemon_cli(old, "hook", "session-start", stdin=json.dumps(payload))
    assert hook.returncode == 0, hook.stderr
    assert "no daemon" not in hook.stderr, hook.stderr
    last = pair.daemon_state()
    assert last is not None, _public(serving)
    assert (last["pid"], last["version"]) == serving_key, _public(last)


def test_this_checkouts_up_replaces_the_previous_releases_daemon_under_its_shim(
    pair: Pair,
) -> None:
    old, new = pair.previous, pair.next
    # f. The previous release's daemon with its shim open, then this
    # checkout's `up`, which replaces the older daemon; the shim follows it.
    ok(pair.daemon_cli(old, "up", "--port", "0"))
    before = pair.wait_daemon(pair.versions[old.name])
    with pair.shim(old) as session:
        session.initialize()
        memory_id = committed(
            write(
                session, "The cross-release fixture rotates its logs weekly.", ["ops"]
            )
        )
        assert memory_id in found(session, "rotates")
        ok(pair.daemon_cli(new, "up", "--port", "0"))
        replaced = pair.wait_daemon(pair.versions[new.name], other_than=before["pid"])
        deadline = time.monotonic() + DAEMON_SECONDS
        while _alive(int(before["pid"])) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(int(before["pid"])), _public(before)
        search = {"query": "rotates", "max_results": 20}
        assert memory_id in hit_ids(_once_more(session, "memory_search", search))
        later = committed(
            write(
                session, "The cross-release fixture archives old releases.", ["release"]
            )
        )
        assert later in found(session, "archives")
        said = session.stderr()
    assert RESTART in said, said
    assert pair.versions[new.name] in said, said
    after = pair.daemon_state()
    assert after is not None, _public(replaced)
    assert after["pid"] == replaced["pid"], _public(after)
