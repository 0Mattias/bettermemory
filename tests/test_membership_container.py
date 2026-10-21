"""I39 to I42 for real (design 7.1 and 8.7, E10): a repository on this
host, containers started from tools/ci/Dockerfile with ``--network none``,
and a daemon on a scratch store at an ephemeral port.

A container runs the hook client's steps (tools/ci/container_client.py,
the package imported from the source tree it mounts read-only): its
namespace probe, its location token, its answers to the daemon's
questions about its repository, a re-mint. This side posts what each step
prints to the daemon and hands the daemon's next question back to the
container, the rounds of `_daemon_client._membership_rounds`, so the
daemon never reads a path of the container (design 8.7). The resolutions
are read from the store's membership rows (design 8.10).

Skipped unless docker is on PATH and ``BETTERMEMORY_CI_DOCKER=1``. The
membership-docker job of .github/workflows/ci.yml runs it on ubuntu-latest,
where a bind mount keeps the host's device and inode; on OrbStack the
virtiofs mount shows another device and inode and no birth time. On a
POSIX host the containers run as the host user's uid and gid, as a
devcontainer's remote user does, so what they write into a mounted
repository is the test's to remove. The image is built once for the
module and removed after it; each container is removed when its test ends.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bettermemory import _daemon_client, location
from bettermemory.config import KEYS_DIR_ENV
from bettermemory.identity import (
    HEADER_HARNESS_SESSION,
    HEADER_SESSION,
    HEADER_WORKSPACE,
)
from bettermemory.membership import line_unreadable
from bettermemory.server import main as cli_main
from bettermemory.store import Store

from .conftest import child_tau_env, shielded_child_env

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("BETTERMEMORY_CI_DOCKER") != "1",
    reason="needs docker on PATH and BETTERMEMORY_CI_DOCKER=1 (the membership-docker "
    "leg of .github/workflows/ci.yml)",
)

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_CI = _ROOT / "tools" / "ci"
_LABEL = "bm.ci=membership-docker"
_URL = "https://example.invalid/acme/devc.git"
#: Where a container sees the host repository. Neither this path nor
#: /work, where a container makes its clones and copies, exists on this
#: host (`_not_here`).
_MOUNT = "/workspaces/proj"
_HOST_SESSION = "s-host"


# ---------------------------------------------------------------------------
# A neutral git
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system configuration reaches git, a fixed author and
    committer, and no variable that points git somewhere else; the daemon
    this test starts inherits the same."""
    empty = tmp_path / "gitconfig-empty"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "fixture@example.invalid")
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_DATE",
        "BETTERMEMORY_WORKSPACE",
        _daemon_client.HARNESS_SESSION_ENV,
    ):
        monkeypatch.delenv(var, raising=False)


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"git {args} failed: {done.stderr.strip()}")
    return done.stdout.strip()


_SEQ = [0]


def make_repo(path: Path, *, commits: int, remote: str) -> Path:
    """A repository with a history of its own (a fresh marker in its
    commits) and `remote` as its origin."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "src").mkdir()
    _SEQ[0] += 1
    marker = f"{os.getpid()}-{_SEQ[0]}-{time.time_ns()}"
    for i in range(commits):
        (path / "src" / f"mod{i}.py").write_text(f"VALUE = {i}\n")
        (path / "README.md").write_text(f"fixture {marker} rev {i}\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"rev {i} {marker}")
    git(path, "remote", "add", "origin", remote)
    return path


def commit(path: Path, message: str) -> str:
    _SEQ[0] += 1
    (path / "src" / f"x{_SEQ[0]}_{time.time_ns()}.py").write_text("X = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", f"{message} {_SEQ[0]}")
    return git(path, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Docker: the image and the containers
# ---------------------------------------------------------------------------


def _docker(*args: str, given: str | None = None, timeout: float = 120.0) -> str:
    done = subprocess.run(
        ["docker", *args],
        input=given,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if done.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args[:3])} exited {done.returncode}: "
            f"{done.stderr.strip()[-2000:]}"
        )
    return done.stdout


def _user() -> list[str]:
    """The host user's uid and gid, on a POSIX host."""
    if sys.platform == "win32":
        return []
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    """The leg's image, built from tools/ci/Dockerfile under a tag of its
    own and removed after the module."""
    try:
        engine = _docker(
            "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"
        ).strip()
    except (OSError, subprocess.SubprocessError, AssertionError) as exc:
        pytest.fail(f"BETTERMEMORY_CI_DOCKER=1 and docker does not answer: {exc}")
    tag = f"bm-ci-membership:{uuid.uuid4().hex[:12]}"
    _docker(
        "build",
        "-q",
        "-t",
        tag,
        "--label",
        _LABEL,
        "-f",
        str(_CI / "Dockerfile"),
        str(_CI),
        timeout=900.0,
    )
    try:
        image_id = _docker("image", "inspect", tag, "--format", "{{.Id}}").strip()
        print(f"docker {engine}; image {tag} {image_id}")
        yield tag
    finally:
        subprocess.run(
            ["docker", "image", "rm", "-f", tag], capture_output=True, check=False
        )


class Box:
    """One container, a machine of its own (design 8.7): started with
    ``--network none`` (only the loopback interface) and kept running while
    its test drives it through ``docker exec``, then removed. `mounts` maps
    a host directory to the path the container sees it at, read-only
    unless marked writable."""

    def __init__(
        self, image: str, mounts: Sequence[tuple[Path, str, bool]] = ()
    ) -> None:
        self.name = f"bm-ci-{uuid.uuid4().hex[:12]}"
        args = [
            "run",
            "-d",
            "--rm",
            "--name",
            self.name,
            "--network",
            "none",
            "--label",
            _LABEL,
            *_user(),
        ]
        for name, value in (
            ("PYTHONPATH", "/bm/src"),
            ("PYTHONDONTWRITEBYTECODE", "1"),
            ("HOME", "/tmp/bm/home"),
            ("BETTERMEMORY_DIR", "/tmp/bm/store"),
            ("BETTERMEMORY_STATE_DIR", "/tmp/bm/state"),
            ("BETTERMEMORY_KEYS_DIR", "/tmp/bm/keys"),
            ("GIT_CONFIG_NOSYSTEM", "1"),
            ("GIT_CONFIG_GLOBAL", "/dev/null"),
            ("GIT_TERMINAL_PROMPT", "0"),
            ("GIT_AUTHOR_NAME", "Fixture"),
            ("GIT_AUTHOR_EMAIL", "fixture@example.invalid"),
            ("GIT_COMMITTER_NAME", "Fixture"),
            ("GIT_COMMITTER_EMAIL", "fixture@example.invalid"),
        ):
            args += ["-e", f"{name}={value}"]
        args += ["-v", f"{_SRC}:/bm/src:ro", "-v", f"{_CI}:/bm/ci:ro"]
        for host, inside, writable in mounts:
            args += ["-v", f"{host}:{inside}" + ("" if writable else ":ro")]
        _docker(*args, image, "sleep", "900")

    def sh(self, script: str) -> str:
        return _docker("exec", self.name, "sh", "-c", script)

    def client(
        self,
        *args: str,
        given: Mapping[str, Any] | None = None,
        workdir: str | None = None,
    ) -> dict[str, Any]:
        """One step of tools/ci/container_client.py, its JSON answer."""
        where = ["-w", workdir] if workdir else []
        out = _docker(
            "exec",
            "-i",
            *where,
            self.name,
            "python3",
            "-B",
            "/bm/ci/container_client.py",
            *args,
            given="" if given is None else json.dumps(given),
        )
        got = json.loads(out)
        assert isinstance(got, dict), out
        return got

    def show(self, path: str) -> dict[str, Any]:
        """What the leg exercised at `path`, printed for the job's log:
        the container's facts, its ``mount`` line and ``stat``."""
        facts = self.client("facts", path)
        print(
            f"container {facts['hostname']} (uid {facts['uid']}, interfaces "
            f"{','.join(facts['interfaces'])}, python {facts['python']}) at {path}: "
            f"device {facts['device']} inode {facts['inode']} birth {facts['birth_ns']}"
        )
        print(self.sh(f"mount | grep ' {_mount_point(facts)} ' ; stat {path}"))
        return facts

    def close(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.name], capture_output=True, check=False
        )

    def __enter__(self) -> Box:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _mount_point(facts: Mapping[str, Any]) -> str:
    line = facts.get("mount")
    return str(line).split()[1] if isinstance(line, str) else "/"


# ---------------------------------------------------------------------------
# The daemon on a scratch store
# ---------------------------------------------------------------------------


class Daemon:
    """A daemon started by ``bettermemory up --port 0`` on the test's
    store, with the test's state and keys directories, a scratch home and
    the review off, as every test starts (conftest's `child_tau_env`)."""

    def __init__(self, store_dir: Path, home: Path, site: Path) -> None:
        self.store_dir = store_dir
        self.state_dir = Path(os.environ[_daemon_client.STATE_DIR_ENV])
        self.keys_dir = Path(os.environ[KEYS_DIR_ENV])
        env = shielded_child_env()
        env.update(
            {
                "BETTERMEMORY_DIR": str(store_dir),
                _daemon_client.STATE_DIR_ENV: str(self.state_dir),
                KEYS_DIR_ENV: str(self.keys_dir),
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "XDG_CACHE_HOME": str(home / ".cache"),
            }
        )
        self.env = child_tau_env(env, site, None)
        up = self._cli("up", "--port", "0")
        assert up.returncode == 0, up.stderr
        self.store_path = (store_dir / _daemon_client.STORE_FILENAME).resolve()
        state = _daemon_client.read_state(self.state_dir, self.store_path)
        assert state is not None, up.stdout + up.stderr
        assert state["port"] != _daemon_client.DEFAULT_PORT
        self.port = int(state["port"])
        self.token = str(state["token"])

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "bettermemory", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def post(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            reply = _daemon_client.post(
                self.port,
                self.token,
                _daemon_client.HOOK_PATH + event,
                dict(payload),
                timeout=60.0,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise AssertionError(f"{event} answered {exc.code}: {body}") from None
        assert "error" not in reply, reply
        return reply

    def rows(self, since: int = 0) -> list[tuple[int, str, dict[str, Any]]]:
        """The membership rows after `since`: (seq, kind, payload)."""
        with Store.open(
            self.store_path, keys_dir=self.keys_dir, allow_rekey=False
        ) as s:
            return [
                (row.seq, row.kind, json.loads(row.payload))
                for row in s.membership_log(since_seq=since)
            ]

    def seq(self) -> int:
        rows = self.rows()
        return rows[-1][0] if rows else 0

    def tool(
        self, name: str, args: Mapping[str, Any], *, workspace: str, harness: str
    ) -> str:
        """One MCP tool call at ``/mcp`` as a shim sends it: its own session,
        the workspace it declares and the harness session it carries
        (`shim.py`). The tool's text."""

        async def call() -> str:
            base = f"http://{_daemon_client.HOST}:{self.port}"
            headers = {
                "Authorization": f"Bearer {self.token}",
                HEADER_SESSION: f"sess_{uuid.uuid4().hex[:16]}",
                HEADER_WORKSPACE: workspace,
                HEADER_HARNESS_SESSION: harness,
            }
            async with httpx2.AsyncClient(
                base_url=base, headers=headers, timeout=60.0
            ) as client:
                async with streamable_http_client(
                    f"{base}/mcp", http_client=client
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        done = await session.call_tool(name, dict(args))
            text = "".join(getattr(block, "text", "") for block in done.content)
            assert not done.is_error, text
            return text

        return asyncio.run(call())

    def write(self, content: str, *, workspace: str, harness: str) -> str:
        """`memory_write` at `workspace`, then `memory_verify` there, since a
        brief shows only rows that read fresh: the new memory's id."""
        text = self.tool(
            "memory_write",
            {"content": content, "scopes": ["tools"]},
            workspace=workspace,
            harness=harness,
        )
        written = json.loads(text)
        assert isinstance(written, dict) and isinstance(written.get("id"), str), text
        memory_id = str(written["id"])
        self.tool(
            "memory_verify",
            {"id": memory_id, "note": "checked when written"},
            workspace=workspace,
            harness=harness,
        )
        return memory_id

    def close(self) -> None:
        self._cli("down")


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Daemon]:
    store_dir = tmp_path / "store"
    # A CLI command this test runs in process opens the daemon's store.
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
    made = Daemon(store_dir, tmp_path / "home", tmp_path / "site")
    try:
        yield made
    finally:
        made.close()


def _of(
    rows: Sequence[tuple[int, str, dict[str, Any]]], kind: str
) -> list[dict[str, Any]]:
    return [payload for _seq, k, payload in rows if k == kind]


def _host_session(daemon: Daemon, repo: Path, session: str) -> dict[str, Any]:
    """A session start in `repo` on this host, as the hook client posts it:
    the daemon reads the same object at the cwd and captures there."""
    return daemon.post(
        "session-start",
        {
            "session_id": session,
            "source": "startup",
            "cwd": str(repo),
            "location": location.probe(str(repo)),
        },
    )


def _host_checkout(daemon: Daemon, repo: Path) -> dict[str, Any]:
    """The first sight of `repo` on this host: its checkout row."""
    since = daemon.seq()
    _host_session(daemon, repo, _HOST_SESSION)
    registered = _of(daemon.rows(since), "checkout_registered")
    assert len(registered) == 1, registered
    assert (repo / ".git" / "info" / "bettermemory").is_file()
    return registered[0]


def rounds(
    daemon: Daemon, box: Box, event: str, payload: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    """The hook client's rounds (`_daemon_client._membership_rounds`), each
    client step run in `box`: the probe, then while the daemon asks, the
    location token and the machine, or the answers to its question; a
    final re-mint where the reply asks for one. The reply and the
    container's machine id."""
    cwd = str(payload["cwd"])
    sent = dict(payload)
    sent["location"] = box.client("probe", cwd)
    reply = daemon.post(event, sent)
    token: dict[str, Any] | None = None
    machine: str | None = None
    policy = False
    answers: dict[str, Any] = {}
    for _ in range(location.EXCHANGE_ROUNDS):
        said = reply.get("membership")
        question = said.get("ask") if isinstance(said, dict) else None
        if not isinstance(question, dict):
            break
        policy = bool(question.get("write_token"))
        if token is None or question.get("location") or question.get("machine"):
            got = box.client("capture", cwd, "1" if policy else "0")
            token, machine = got["location"], got["machine"]["id"]
            sent["location"], sent["machine"] = got["location"], got["machine"]
        else:
            answers = box.client(
                "answer",
                given={"question": question, "answers": answers, "location": token},
            )
            sent["answers"] = answers
        reply = daemon.post(event, sent)
    said = reply.get("membership")
    assert not (isinstance(said, dict) and "ask" in said), said
    if isinstance(said, dict) and said.get("remint") and token is not None:
        wrote = box.client("remint", given={"location": token, "policy": policy})
        print(f"re-minted in {box.name} at {cwd}: {wrote['status']}")
    assert machine is not None, "the daemon never asked for the location token"
    return reply, machine


def _no_question(reply: Mapping[str, Any]) -> None:
    """A silent resolution: the session's first output carries no
    membership line (design 5.4)."""
    assert not any(
        line.startswith("bettermemory:") for line in str(reply["stdout"]).splitlines()
    ), reply["stdout"]


def _not_here(*paths: str) -> None:
    """The container's paths are paths this host does not have."""
    for path in paths:
        assert not os.path.lexists(path), f"{path} exists on this host"


# ---------------------------------------------------------------------------
# I39 to I42
# ---------------------------------------------------------------------------


def test_i39_a_bind_mount_is_a_checkout_of_the_project_per_container_machine(
    tmp_path: Path, daemon: Daemon, image: str
) -> None:
    """I39 (E10 a): the host repository bind-mounted into a container. The
    daemon cannot read the container's path; the location token carries
    the host checkout's token, the container checks the project's commits
    and the remote is the project's: T+H+R, silent, a checkout of the
    project for the container's machine. The next session there finds
    that checkout again."""
    _not_here(_MOUNT)
    host = make_repo(tmp_path / "host" / "proj", commits=2, remote=_URL)
    hk = _host_checkout(daemon, host)
    st = os.stat(host / ".git")
    with Box(image, [(host, _MOUNT, True)]) as box:
        facts = box.show(f"{_MOUNT}/.git")
        same = (facts["device"], facts["inode"]) == (st.st_dev, st.st_ino)
        print(
            f"host git directory: device {st.st_dev} inode {st.st_ino}; through "
            "the bind mount: "
            + ("the same device and inode" if same else "another device and inode")
        )
        assert facts["interfaces"] == ["lo"]
        since = daemon.seq()
        reply, machine = rounds(
            daemon,
            box,
            "session-start",
            {"session_id": "s-container-a", "source": "startup", "cwd": _MOUNT},
        )
        rows = daemon.rows(since)
        _no_question(reply)
        assert _of(rows, "proposal_opened") == []
        registered = _of(rows, "checkout_registered")
        assert len(registered) == 1, registered
        k = registered[0]
        assert k["machine"] == machine != hk["machine"]
        assert (k["project"], k["path"]) == (hk["project"], _MOUNT)
        assert k["checkout"] != hk["checkout"]
        joined = _of(rows, "resolution_joined")
        assert [(j["checkout"], j["witnesses"]) for j in joined] == [
            (k["checkout"], ["H", "R", "T"])
        ]

        since = daemon.seq()
        again, again_machine = rounds(
            daemon,
            box,
            "session-start",
            {"session_id": "s-container-a2", "source": "startup", "cwd": _MOUNT},
        )
        rows = daemon.rows(since)
        _no_question(again)
        assert again_machine == machine
        assert _of(rows, "checkout_registered") == []
        assert _of(rows, "proposal_opened") == []


def test_i40_a_clone_inside_and_a_copy_the_host_cannot_see(
    tmp_path: Path, daemon: Daemon, image: str
) -> None:
    """I40 (E10 b, e): a clone made inside a container joins by history
    and remote (H+R) with a token of its own; a copy made inside another
    container at a path the host cannot see, its remote removed, joins by
    the token captured inside and the history (T+H). Both silent."""
    _not_here("/work/proj2", "/work/copy")
    host = make_repo(tmp_path / "host" / "proj", commits=2, remote=_URL)
    hk = _host_checkout(daemon, host)
    host_token = (host / ".git" / "info" / "bettermemory").read_text()
    found: list[tuple[str, str, str]] = []
    with Box(image, [(host, _MOUNT, False)]) as box:
        box.sh(
            f"git clone -q {_MOUNT} /work/proj2 && "
            f"git -C /work/proj2 remote set-url origin {_URL}"
        )
        box.show("/work/proj2/.git")
        since = daemon.seq()
        reply, machine = rounds(
            daemon,
            box,
            "session-start",
            {"session_id": "s-container-b", "source": "startup", "cwd": "/work/proj2"},
        )
        rows = daemon.rows(since)
        _no_question(reply)
        assert _of(rows, "proposal_opened") == []
        (k,) = _of(rows, "checkout_registered")
        found.append((k["rule"], k["project"], k["machine"]))
        assert k["machine"] == machine
        inside = box.sh("cat /work/proj2/.git/info/bettermemory")
        assert inside.strip() and inside != host_token
    with Box(image, [(host, _MOUNT, False)]) as box:
        box.sh(f"cp -a {_MOUNT} /work/copy && git -C /work/copy remote remove origin")
        box.show("/work/copy/.git")
        since = daemon.seq()
        reply, machine = rounds(
            daemon,
            box,
            "session-start",
            {"session_id": "s-container-e", "source": "startup", "cwd": "/work/copy"},
        )
        rows = daemon.rows(since)
        _no_question(reply)
        assert _of(rows, "proposal_opened") == []
        (k,) = _of(rows, "checkout_registered")
        found.append((k["rule"], k["project"], k["machine"]))
        assert k["machine"] == machine
    assert [(rule, project) for rule, project, _m in found] == [
        ("join:H+R", hk["project"]),
        ("join:H+T", hk["project"]),
    ]
    assert len({m for _r, _p, m in found} | {hk["machine"]}) == 3


class _Terminal(io.StringIO):
    """A stdin that is a terminal, holding what the user types."""

    def isatty(self) -> bool:
        return True


def _project_init(monkeypatch: pytest.MonkeyPatch, repo: Path, name: str) -> None:
    """`bettermemory project init --name <name>` typed at a terminal in
    `repo` (decision D6): the CLI in process, beside the daemon, on its
    store, the name typed to confirm."""
    monkeypatch.chdir(repo)
    monkeypatch.setattr("getpass.getuser", lambda: "tester")
    monkeypatch.setattr(sys, "stdin", _Terminal(name + "\n"))
    monkeypatch.setattr(
        sys, "argv", ["bettermemory", "project", "init", "--name", name]
    )
    cli_main()


def test_i41_throwaway_containers_with_the_committed_file_join_without_a_question(
    tmp_path: Path,
    daemon: Daemon,
    image: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """I41 (E10 c): the project file committed, then a tip no witness
    names. Two throwaway containers each make a shallow clone: the first
    joins by the file and the remote (F+R), the second, the tip a witness
    now, by the file, the history and the remote (F+H+R); silent, no
    question, each its own machine."""
    _not_here("/work/p")
    host = make_repo(tmp_path / "host" / "proj", commits=2, remote=_URL)
    hk = _host_checkout(daemon, host)
    _project_init(monkeypatch, host, "devc")
    said = capsys.readouterr().out
    assert "bettermemory: wrote" in said, said
    git(host, "add", "-A")
    git(host, "commit", "-q", "-m", "project file")
    _host_session(daemon, host, "s-host-2")
    commit(host, "a tip no witness names")
    since = daemon.seq()
    found: list[tuple[str, str, str]] = []
    for i in (1, 2):
        with Box(image, [(host, _MOUNT, False)]) as box:
            box.sh(
                f"git clone -q --depth 1 file://{_MOUNT} /work/p && "
                f"git -C /work/p remote set-url origin {_URL}"
            )
            if i == 1:
                box.show("/work/p/.git")
            before = daemon.seq()
            reply, machine = rounds(
                daemon,
                box,
                "session-start",
                {
                    "session_id": f"s-throwaway-{i}",
                    "source": "startup",
                    "cwd": "/work/p",
                },
            )
            _no_question(reply)
            (k,) = _of(daemon.rows(before), "checkout_registered")
            assert k["machine"] == machine
            found.append((k["rule"], k["project"], k["machine"]))
    assert [(rule, project) for rule, project, _m in found] == [
        ("join:F+R", hk["project"]),
        ("join:F+H+R", hk["project"]),
    ]
    assert len({m for _r, _p, m in found} | {hk["machine"]}) == 3
    assert _of(daemon.rows(since), "proposal_opened") == []


def test_i42_an_rc3_client_posting_a_container_path_gives_l8(
    tmp_path: Path, daemon: Daemon, image: str
) -> None:
    """I42 (E10 a control): an rc3 hook client in the container posts its
    bare cwd and no location. The daemon cannot read that path: line L8
    for that prompt, nothing from any project, a
    ``resolution_indeterminate`` row; no checkout is registered."""
    host = make_repo(tmp_path / "host" / "proj", commits=2, remote=_URL)
    _host_checkout(daemon, host)
    with Box(image, [(host, _MOUNT, False)]) as box:
        payload = box.client("rc3", workdir=_MOUNT)
    assert payload == {"cwd": _MOUNT}
    _not_here(payload["cwd"])
    since = daemon.seq()
    reply = daemon.post(
        "prompt",
        {
            **payload,
            "session_id": "s-rc3",
            "prompt": "which base image does the devcontainer build from?",
        },
    )
    rows = daemon.rows(since)
    assert "membership" not in reply
    (row,) = _of(rows, "resolution_indeterminate")
    assert row["cwd"] == _MOUNT
    assert reply["stdout"] == line_unreadable(_MOUNT, row["detail"])
    assert reply["ids"] == []
    assert _of(rows, "checkout_registered") == []
    print(reply["stdout"])


def test_e10_d_records_cross_between_the_host_path_and_the_container_path(
    tmp_path: Path, daemon: Daemon, image: str
) -> None:
    """E10 a, d with delivery (design 3.5, 8.7): the host checkout and the
    container's view of it through the bind mount are two checkouts of one
    project. The container's resolution names the host checkout's project
    and mints none. Through the daemon, a memory the container's client
    writes at the mounted path is delivered to a session at the host path
    (its brief, and `memory_search` with auto_scope on), and one written
    at the host path is delivered to the container's session brief. A
    memory of another project on this host is delivered to neither. The
    container's MCP calls are the ones its shim sends (the workspace it
    declares, its harness session), relayed from this side, since the
    container has no network."""
    _not_here(_MOUNT)
    host = make_repo(tmp_path / "host" / "proj", commits=2, remote=_URL)
    other = make_repo(
        tmp_path / "host" / "other",
        commits=2,
        remote="https://example.invalid/acme/other.git",
    )
    hk = _host_checkout(daemon, host)
    other_k = _host_checkout(daemon, other)
    assert other_k["project"] != hk["project"]
    with Box(image, [(host, _MOUNT, True)]) as box:
        since = daemon.seq()
        reply, machine = rounds(
            daemon,
            box,
            "session-start",
            {"session_id": "s-container-d", "source": "startup", "cwd": _MOUNT},
        )
        rows = daemon.rows(since)
        _no_question(reply)
        assert _of(rows, "project_minted") == []
        (k,) = _of(rows, "checkout_registered")
        assert (k["project"], k["machine"]) == (hk["project"], machine)

        from_host = daemon.write(
            "kestrel: the devcontainer image is built from python alpine with git",
            workspace=str(host),
            harness="s-host-w",
        )
        from_container = daemon.write(
            "kestrel: the devcontainer mounts the checkout read and write",
            workspace=_MOUNT,
            harness="s-container-d",
        )
        elsewhere = daemon.write(
            "kestrel: the other project pins its own base image",
            workspace=str(other),
            harness="s-other-w",
        )
        # The container session's next prompt resolves the mounted path
        # again: the write it made there while the daemon could not read
        # that path joins the checkout's project (decision B6).
        rounds(
            daemon,
            box,
            "prompt",
            {
                "session_id": "s-container-d",
                "cwd": _MOUNT,
                "prompt": "which base image does the devcontainer build from?",
            },
        )
        print(
            f"written: host {from_host}, container {from_container}, other {elsewhere}"
        )

        at_host = _host_session(daemon, host, "s-host-brief")
        found = daemon.tool(
            "memory_search",
            {"query": "kestrel devcontainer base image", "auto_scope": True},
            workspace=str(host),
            harness="s-host-brief",
        )
        inside, _machine = rounds(
            daemon,
            box,
            "session-start",
            {"session_id": "s-container-brief", "source": "startup", "cwd": _MOUNT},
        )
    print(f"host brief ids {at_host['ids']}; container brief ids {inside['ids']}")
    with Store.open(
        daemon.store_path, keys_dir=daemon.keys_dir, allow_rekey=False
    ) as s:
        homes = {r["record_id"]: r for r in s.membership_rows("homes")}
        merged = {r["id"]: r["merged_into"] for r in s.membership_rows("projects")}
    home = homes[from_container]
    print(f"the container's write is homed: {home}")
    seen = {
        "container write homed in the host checkout's project": hk["project"]
        in (home["project"], merged.get(home["project"])),
        "container write in the host brief": from_container in at_host["ids"],
        "container write in the host search": from_container in found,
        "host write in the container brief": from_host in inside["ids"],
        "other project in the host brief": elsewhere in at_host["ids"],
        "other project in the host search": elsewhere in found,
        "other project in the container brief": elsewhere in inside["ids"],
    }
    assert seen == {
        "container write homed in the host checkout's project": True,
        "container write in the host brief": True,
        "container write in the host search": True,
        "host write in the container brief": True,
        "other project in the host brief": False,
        "other project in the host search": False,
        "other project in the container brief": False,
    }
