"""The previous published release's hook client against this checkout's
daemon, over the witness exchange (design 8.7; ENG-213, ENG-216).

tests/test_cross_release.py drives both builds as a user's install meets
them, as the CLI, the MCP tools and a daemon. The exchange runs only where
the daemon cannot read the client's paths, a container's, which one CI
host does not stand up there. So here the daemon's half is this
checkout's `location.resolve_hook`, called in process on a store under the
test's own directory, as tests/test_membership_resolution.py calls it,
and the client's half is the previous release itself: each step of the
hook client's rounds (`_daemon_client._membership_rounds`), its capture
and machine and its answer to each question, runs
tools/ci/container_client.py under the previous release's interpreter
with ``-I``, so only that install is imported, in a sandbox under the
test's directory (`Previous.env`). The container is played as the
membership tests play one (`Container`): the paths of the client's
location token are respelled under a mount the daemon cannot read, and a
view at the mount reads as live, the one answer the client does not make
(this host has no such mount). CI's cross-release job names the previous
release's ``bettermemory`` in ``BETTERMEMORY_CI_PREVIOUS``; without it the
module is skipped.

Two exchanges run. A tie past the traversal's bound (ENG-216): a client
that says it answers git's reading of tied witnesses (``independent_ok``)
is asked it once and orders the tie as the host does; one that does not
is never asked, and its traversal asks which project, as it did. And a
part's folder whose declaration stored another spelling (ENG-213), which
resolves to the part, its folder read once where the client names the
exchange back and each round where it does not. Either settles within the
client's rounds, and nothing the daemon kept for the exchange outlives it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location, location_witness
from bettermemory.location import Exchange, resolve_hook
from bettermemory.membership import ASK, SILENT, Declaration, Resolution

from .test_cross_release import CARRIED, PREVIOUS_ENV, Build, _search_path, _under
from .test_membership_hung_view import _Below, _declared_reads, _folder_with_a_part
from .test_membership_resolution import (
    _DEVC,
    _PRINCIPAL,
    Container,
    Lab,
    _as_the_client,
    _container_machine,
    clone,
    git,
    make_repo,
)
from .test_membership_resolution import neutral_git  # noqa: F401

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get(PREVIOUS_ENV),
        reason=f"needs {PREVIOUS_ENV} (the cross-release job)",
    ),
    pytest.mark.skipif(os.name == "nt", reason="the cross-release job runs on POSIX"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH"),
]

#: The container's half of the exchange, one step a run.
CLIENT = Path(__file__).resolve().parents[1] / "tools" / "ci" / "container_client.py"
#: The bound on one step of the client.
STEP_SECONDS = 60.0
#: Commits past the template's witness on the product's line: more than the
#: traversal reads (`location_git.WALK_LIMIT`, 512), so only git orders them.
DEPTH = 700


@dataclass(frozen=True)
class Previous:
    """The previous release's hook client, its files under `root`."""

    build: Build
    root: Path

    def env(self, machine: str) -> dict[str, str]:
        """The whole environment of a step of the client on `machine`: an
        allowlist, every path the package or git reads inside `root`, and
        the machine's own state directory."""
        env = {name: os.environ[name] for name in CARRIED if name in os.environ}
        env.update(
            {
                "PATH": _search_path(self.build),
                "HOME": str(self.root / "home"),
                "TMPDIR": str(self.root / "tmp"),
                "BETTERMEMORY_DIR": str(self.root / "store"),
                "BETTERMEMORY_KEYS_DIR": str(self.root / "keys"),
                "BETTERMEMORY_STATE_DIR": str(self.root / f"{machine}-state"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return env

    def run(
        self, machine: str, *args: str, given: Mapping[str, Any] | None = None
    ) -> subprocess.CompletedProcess[str]:
        for name in ("home", "tmp"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [str(self.build.bin_dir / "python"), "-I", *args],
            input="" if given is None else json.dumps(given),
            env=self.env(machine),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=STEP_SECONDS,
            check=False,
        )

    def check(self) -> str:
        """The version of the package the client's interpreter imports,
        failing unless it is the previous release's install."""
        done = self.run(
            "check",
            "-c",
            "import importlib.metadata, json, bettermemory; print(json.dumps("
            "[bettermemory.__file__, importlib.metadata.version('bettermemory')]))",
        )
        assert done.returncode == 0, done.stderr
        where, version = json.loads(done.stdout)
        assert _under(where, self.build.venv), f"the client imports {where}"
        print(f"the previous release's client: bettermemory {version} at {where}")
        return str(version)

    def step(
        self, machine: str, *args: str, given: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """One step of the container's half (tools/ci/container_client.py)
        on `machine`, its JSON answer."""
        done = self.run(machine, str(CLIENT), *args, given=given)
        assert done.returncode == 0, done.stderr
        got = json.loads(done.stdout)
        assert isinstance(got, dict), done.stdout
        return got

    def exchange(
        self, lab: Lab, box: Container, session: str
    ) -> tuple[Resolution | Exchange, list[dict[str, Any]], dict[str, Any]]:
        """The hook client's rounds as `_daemon_client._membership_rounds`
        makes them, against `resolve_hook`: `box`'s probe posted, then at
        most `location.EXCHANGE_ROUNDS` posts more. Where the daemon asks
        for the location and the machine, the previous release captures
        `box.real` and opens its machine (named by `box.hostname`) and
        posts both, the token respelled as `box` posts one; else it
        answers the question from its capture, beside its earlier answers,
        and posts them with the location. The last reply, the questions
        answered and the last answers."""
        machine = box.hostname
        posted: dict[str, Any] = {"location": box.probe()}
        reply = resolve_hook(lab.store, lab.config, box.mount, posted, session=session)
        token: dict[str, Any] | None = None
        answers: dict[str, Any] = {}
        asked: list[dict[str, Any]] = []
        for _ in range(location.EXCHANGE_ROUNDS):
            if not isinstance(reply, Exchange):
                break
            question = dict(reply.question)
            if token is None or question.get("location") or question.get("machine"):
                policy = "1" if question.get("write_token") else "0"
                got = self.step(machine, "capture", str(box.real), policy)
                token = got["location"]
                posted = {"machine": got["machine"]}
            else:
                asked.append(question)
                answers = self.answer(box, question, answers, token)
                posted["answers"] = answers
            posted["location"] = box.posted(json.loads(json.dumps(token)))
            reply = resolve_hook(
                lab.store, lab.config, box.mount, posted, session=session
            )
        return reply, asked, answers

    def answer(
        self,
        box: Container,
        question: Mapping[str, Any],
        answers: Mapping[str, Any],
        token: Mapping[str, Any],
    ) -> dict[str, Any]:
        """The previous release's answers to `question` from its capture
        (`token`), each view the question names read as `box` reads one:
        live under the folder the container mounts, gone elsewhere."""
        views = [v for v in question.get("views") or () if isinstance(v, Mapping)]
        given = {
            "question": {k: v for k, v in question.items() if k != "views"},
            "answers": answers,
            "location": token,
        }
        out = self.step(box.hostname, "answer", given=given)
        mounted = getattr(box, "at", "") or box.mount
        states = dict(out.get("views") or {})
        for view in views:
            path = str(view["path"])
            mine = path == mounted or path.startswith(mounted + "/")
            states[path] = "live" if mine else "gone"
        out["views"] = states
        return out


@pytest.fixture
def previous(tmp_path: Path) -> Previous:
    build = Build("previous", Path(os.environ[PREVIOUS_ENV]).absolute())
    client = Previous(build, tmp_path / "previous")
    client.check()
    return client


@pytest.fixture
def labs(local_tmp_path: Path) -> Iterator[Callable[[str], Lab]]:
    """Scenarios of this checkout's daemon, each a `Lab` named for it,
    closed when the test ends."""
    made: list[Lab] = []

    def make(name: str) -> Lab:
        made.append(Lab(local_tmp_path / name))
        return made[-1]

    yield make
    for each in made:
        each.close()


def _appended(repo: Path, count: int) -> str:
    """`count` empty commits on `repo`'s main past HEAD, a minute apart, by
    fast-import; the new HEAD."""
    head = git(repo, "rev-parse", "HEAD")
    start = int(time.time()) + 60
    stream: list[str] = []
    for i in range(1, count + 1):
        message = f"step {i}"
        stream += [
            "commit refs/heads/main",
            f"mark :{i}",
            f"committer Fixture <fixture@example.invalid> {start + 60 * i} +0000",
            f"data {len(message)}",
            message,
            f"from {head}" if i == 1 else f"from :{i - 1}",
            "",
        ]
    subprocess.run(
        ["git", "fast-import", "--quiet", "--force"],
        input=("\n".join(stream) + "\n").encode(),
        cwd=repo,
        capture_output=True,
        check=True,
    )
    git(repo, "reset", "-q", "--hard", "main")
    return git(repo, "rev-parse", "HEAD")


def test_a_tie_past_the_bound_resolves_with_the_previous_releases_client(
    tmp_path: Path, labs: Callable[[str], Lab], previous: Previous
) -> None:
    """ENG-216 across releases: a template, a product cloned from it and
    declared a project of its own, `DEPTH` commits on the product, so a new
    clone holds both projects' witnesses further apart than the traversal
    reads. On the host git orders the tie and the clone joins the product.
    The previous release's client in a container settles the exchange
    within its rounds: one that says it answers git's reading is asked it
    once and joins as the host does; one that does not is never asked,
    and its traversal, cut short, asks which project, as it did. Nothing
    the daemon kept for the exchange is left."""
    lab = labs("tie")
    template = make_repo(tmp_path / "host" / "template", commits=2, remote=_DEVC)
    lab.write("t1", template)
    product = clone(template, tmp_path / "host" / "product", url=_DEVC)
    joined = lab.caller(product).resolution
    assert joined.checkout is not None
    declared = lab._declaring().declare(
        Declaration("new", _PRINCIPAL, checkout=joined.checkout, name="product")
    )
    location.forget()
    _appended(product, DEPTH)
    lab.write("p1", product)
    assert lab.project_of("p1") == declared.project != lab.project_of("t1")
    host = lab.caller(clone(product, tmp_path / "host" / "fresh", url=_DEVC))
    box = Container(
        clone(product, tmp_path / "box" / "fresh", url=_DEVC),
        "/work/fresh",
        _container_machine(lab, "container-a"),
    )
    kept = dict(location_witness._EXCHANGES)

    res, asked, answers = previous.exchange(lab, box, "s-tie")

    shown = [sorted(set(q) - {"exchange", "write_token"}) for q in asked]
    assert isinstance(res, Resolution), f"no resolution in the client's rounds: {shown}"
    tied = [q for q in asked if "independent" in q]
    assert (host.resolution.outcome, host.resolution.project) == (
        SILENT,
        declared.project,
    )
    if answers.get("independent_ok") is True:
        assert len(tied) == 1, shown
        assert (res.outcome, res.project, res.rule) == (
            SILENT,
            declared.project,
            host.resolution.rule,
        )
    else:
        assert tied == [], shown
        assert any("ancestry" in q for q in asked), shown
        assert (res.outcome, res.rule) == (ASK, "ask:several_projects")
        assert set(res.candidates) == {declared.project, lab.project_of("t1")}
    assert dict(location_witness._EXCHANGES) == kept
    print(f"tie: {res.outcome} {res.rule} over {shown}")


def _in_the_part(tmp_path: Path, lab: Lab) -> tuple[Container, _Below, str]:
    """`_folder_with_a_part` under `tmp_path` on `lab`'s store: a folder
    project and a part of it declared below, the part's folder stored
    through a link, as a release before 9.0.0rc14 stored one. A container
    client that mounts the enclosing folder, one standing in the part's
    folder below it, on one machine, and the stored folder."""
    here, stored, _part = _folder_with_a_part(tmp_path, lab.store, lab.config)
    identity = _container_machine(lab, "box")
    outer = Container(here.parent, "/work/Writing", identity, hostname="box")
    box = _Below(
        here,
        "/work/Writing/novel",
        identity,
        hostname="box",
        base=here.parent,
        at=outer.mount,
    )
    return outer, box, stored


def _said(res: Resolution | Exchange) -> tuple[str, str, str | None, list[str]]:
    """What a resolution says, to compare two runs of one scenario."""
    assert isinstance(res, Resolution), f"no resolution in the client's rounds: {res}"
    return res.outcome, res.rule, res.project, sorted(res.candidates)


def test_a_declared_folder_resolves_with_the_previous_releases_client(
    tmp_path: Path,
    labs: Callable[[str], Lab],
    previous: Previous,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENG-213 across releases: a container client sees a folder project,
    then stands in the folder of a part declared below it, whose
    declaration stored the folder in another spelling (`_in_the_part`).
    The daemon reads the stored folder in its own namespace beside the
    views it asks the client about. The previous release's client settles
    both exchanges within its rounds, each as this checkout's own client
    (`_as_the_client`) settles them in the same scenario on a store of its
    own. A client that names the exchange back has the folder read once
    in the part's exchange; one that does not, in each round, as before.
    Nothing the daemon kept for either exchange is left."""
    mine = labs("this")
    outer, box, _stored = _in_the_part(tmp_path / "this", mine)
    seen_here = _as_the_client(outer, mine, "s-box")[0]
    res_here = _as_the_client(box, mine, "s-box")[0]
    lab = labs("previous")
    outer, box, stored = _in_the_part(tmp_path / "previous", lab)
    kept = dict(location_witness._EXCHANGES)

    seen = previous.exchange(lab, outer, "s-box")[0]
    reads = _declared_reads(lab.store, monkeypatch)
    res, asked, answers = previous.exchange(lab, box, "s-box")

    assert _said(seen) == _said(seen_here)
    assert _said(res) == _said(res_here)
    once = "exchange" in answers
    assert [p for p, _held in reads] == [stored] * (1 if once else len(asked) + 1)
    assert dict(location_witness._EXCHANGES) == kept
    shown = [sorted(set(q) - {"exchange", "write_token"}) for q in asked]
    print(f"part: {_said(res)[:2]} over {shown}, its folder read {len(reads)} times")
