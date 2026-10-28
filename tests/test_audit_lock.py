"""`tools/audit_lock.py` and `tools/_uvlock.py`: the OSV audit of uv.lock
that `.github/workflows/audit.yml` runs, and the lock reader it shares with
`tools/sbom.py`.

Nothing here touches the network. OSV's answers come from
`tests/fixtures/osv/osv.json` (``--offline``) or from a fake opener, over
the synthetic lock `tests/fixtures/osv/lock.toml`. One opt-in test asks the
real OSV about the real lock (``BETTERMEMORY_TEST_NETWORK=1``).
"""

from __future__ import annotations

import datetime as dt
import email.message
import importlib.util
import io
import json
import os
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "osv"
LOCK = FIXTURES / "lock.toml"
OSV = FIXTURES / "osv.json"
TODAY = dt.date(2026, 10, 28)


def _load(name: str) -> ModuleType:
    """A tools/ script as a module, registered under its own name so its
    dataclasses resolve and so a sibling's ``import _uvlock`` finds this
    copy rather than needing tools/ on sys.path."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


uvlock = _load("_uvlock")
audit_lock = _load("audit_lock")


def _lock() -> Any:
    return uvlock.load(LOCK)


def _ignores(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "audit-ignore.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _run(
    capsys: pytest.CaptureFixture[str], *args: str, today: dt.date = TODAY
) -> tuple[int, str, str]:
    code = audit_lock.main(
        ["--lock", str(LOCK), "--offline", str(OSV), *args], today=today
    )
    out, err = capsys.readouterr()
    return code, out, err


# ------------------------------------------------------------- the lock


def test_the_runtime_set_follows_extras_and_takes_every_marker() -> None:
    """jwtlib[crypto] brings cryptokit; the Windows-only winshim is in; the
    fork's 1.0.0 is runtime (webframe names it) and its 2.0.0 is not."""
    lock = _lock()
    root = lock.root()
    assert root.name == "demo"
    runtime = {(name, version) for name, version, _source in lock.runtime(root)}
    assert runtime == {
        ("webframe", "2.0.0"),
        ("internal-lib", "1.2.0"),
        ("parser", "1.0.0"),
        ("jwtlib", "3.1.0"),
        ("cryptokit", "42.0.0"),
        ("winshim", "306"),
        ("vendored", "0.1.0"),
    }
    assert lock.runtime(root)[
        next(k for k in lock.runtime(root) if k[0] == "jwtlib")
    ] == {"crypto"}


def test_everything_else_is_dev_and_the_root_is_neither() -> None:
    """The dev extra, the lint group, what they pull in, and a package no
    edge reaches are all dev; the project itself has no label."""
    lock = _lock()
    root = lock.root("demo")
    sets = {(k[0], k[1]): label for k, label in lock.sets(root).items()}
    assert ("demo", "1.0.0") not in sets
    dev = {key for key, label in sets.items() if label == "dev"}
    assert dev == {
        ("testrunner", "8.0.0"),
        ("colorlib", "0.4.6"),
        ("parser", "2.0.0"),
        ("lintkit", "0.5.0"),
        ("orphan", "0.0.1"),
    }


@pytest.mark.parametrize(
    "edges", ['{ name = "a" }, { name = "b" }', '{ name = "b" }, { name = "a" }']
)
def test_an_extra_met_late_is_still_followed(edges: str) -> None:
    """A package first reached without an extra and later with one has the
    extra's dependencies followed then, whatever the edge order."""
    text = f"""
version = 1
revision = 3
[[package]]
name = "app"
version = "1"
source = {{ virtual = "." }}
dependencies = [{edges}]"""
    text += """
[[package]]
name = "a"
version = "1"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "b", extra = ["x"] }]
[[package]]
name = "b"
version = "1"
source = { registry = "https://pypi.org/simple" }
[package.optional-dependencies]
x = [{ name = "c" }]
[[package]]
name = "c"
version = "1"
source = { registry = "https://pypi.org/simple" }
"""
    lock = uvlock.parse(text)
    names = {key[0] for key in lock.runtime(lock.root())}
    assert names == {"a", "b", "c"}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not toml [", "not TOML"),
        ("version = 2\n", "lock format version 2"),
        (
            'version = 1\n[[package]]\nname = "x"\nsource = { registry = "r" }\n',
            "registry package has no version",
        ),
        (
            'version = 1\n[[package]]\nname = "x"\nversion = "1"\nsource = { weird = "?" }\n',
            "names no kind",
        ),
        (
            'version = 1\n[[package]]\nname = "app"\nversion = "1"\nsource = { editable = "." }\n'
            'dependencies = [{ name = "ghost" }]\n',
            "does not hold",
        ),
    ],
)
def test_a_lock_the_reader_cannot_trust_is_refused(text: str, message: str) -> None:
    with pytest.raises(uvlock.LockError, match=message):
        lock = uvlock.parse(text)
        lock.runtime(lock.root())


def test_a_missing_lock_is_a_lock_error(tmp_path: Path) -> None:
    with pytest.raises(uvlock.LockError, match="missing.lock"):
        uvlock.load(tmp_path / "missing.lock")


def test_the_project_table_gives_name_version_and_license(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "Demo_App"\nversion = "1.2"\nlicense = { text = "MIT" }\n',
        encoding="utf-8",
    )
    assert uvlock.project(pyproject) == ("demo-app", "1.2", "MIT")
    pyproject.write_text(
        '[project]\nname = "demo"\ndynamic = ["version"]\nlicense = "Apache-2.0"\n',
        encoding="utf-8",
    )
    assert uvlock.project(pyproject) == ("demo", None, "Apache-2.0")


def test_the_repositorys_own_lock_reads() -> None:
    """The real uv.lock parses, names the project, and every package is
    labelled; the audit's package list covers every PyPI package in it."""
    lock = uvlock.load(REPO / "uv.lock")
    name, _version, _license = uvlock.project(REPO / "pyproject.toml")
    root = lock.root(name)
    assert root.name == "bettermemory"
    audited, skipped = audit_lock.locked_packages(lock, root, "all")
    assert skipped == []
    assert len(audited) == len(lock.packages) - 1
    assert {p.set for p in audited} == {"runtime", "dev"}


# ---------------------------------------------------------- the ignore file


def test_no_ignore_file_is_no_entries(tmp_path: Path) -> None:
    assert audit_lock.load_ignores(tmp_path / "absent.toml") == []


def test_the_repositorys_ignore_file_loads() -> None:
    """Shipped with no live entries; it must parse, or the workflow exits 2."""
    assert audit_lock.load_ignores(TOOLS / "audit-ignore.toml") == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[[ignore]]\nreason = "r"\nexpires = 2026-12-31\n', "has no id"),
        ('[[ignore]]\nid = "X-1"\nexpires = 2026-12-31\n', "has no reason"),
        ('[[ignore]]\nid = "X-1"\nreason = "r"\n', "has no expires date"),
        ('[[ignore]]\nid = "X-1"\nreason = "r"\nexpires = "soon"\n', "expires 'soon'"),
        (
            '[[ignore]]\nid = "X-1"\nreason = "r"\nexpires = 2026-12-31T10:00:00\n',
            "no expires",
        ),
        (
            '[[ignore]]\nid = "X-1"\nreason = "r"\nexpires = 2026-12-31\n'
            '[[ignore]]\nid = "x-1"\nreason = "again"\nexpires = 2026-12-31\n',
            "listed twice",
        ),
        ("ignore = 3\n", "array of tables"),
        ("not toml [", "audit-ignore.toml"),
    ],
)
def test_a_half_written_entry_is_refused(
    tmp_path: Path, body: str, message: str
) -> None:
    """An acceptance missing its id, reason or date is an error, never a
    silent pass."""
    with pytest.raises(audit_lock.AuditError, match=message):
        audit_lock.load_ignores(_ignores(tmp_path, body))


def test_an_expiry_may_be_a_date_or_a_string(tmp_path: Path) -> None:
    entries = audit_lock.load_ignores(
        _ignores(
            tmp_path,
            '[[ignore]]\nid = "A-1"\nreason = "r"\nexpires = 2026-12-31\n'
            '[[ignore]]\nid = "B-1"\nreason = "r"\nexpires = "2027-01-15"\npackage = "Web_Frame"\n',
        )
    )
    assert [(e.id, e.expires, e.package) for e in entries] == [
        ("A-1", dt.date(2026, 12, 31), None),
        ("B-1", dt.date(2027, 1, 15), "Web_Frame"),
    ]


# ------------------------------------------------------------ the audit


def test_a_hit_fails_and_aliases_fold_into_one_finding(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """OSV names webframe's vulnerability twice (PYSEC and GHSA); the
    report shows it once, led by the GHSA record, with every id as an alias
    and both records' fixed versions."""
    code, out, _err = _run(
        capsys, "--format", "json", "--ignore-file", str(tmp_path / "none.toml")
    )
    assert code == 1
    report = json.loads(out)
    assert report["status"] == "fail"
    assert report["packages"] == {"runtime": 5, "dev": 5}
    assert report["skipped"] == [
        "internal-lib 1.2.0 (registry https://pkgs.example.invalid/simple)",
        "vendored 0.1.0 (git https://git.example.invalid/vendored.git"
        "?rev=main#0123456789abcdef0123456789abcdef01234567)",
    ]
    webframe = [f for f in report["findings"] if f["package"] == "webframe"]
    assert len(webframe) == 1
    finding = webframe[0]
    assert finding["id"] == "GHSA-test-0001-aaaa"
    assert finding["aliases"] == ["CVE-0000-0001", "PYSEC-0000-1"]
    assert finding["fixed"] == ["2.0.1", "3.0.2"]
    assert finding["severity"] == "HIGH"
    assert finding["set"] == "runtime"
    assert finding["status"] == "open"
    assert finding["url"] == "https://osv.dev/vulnerability/GHSA-test-0001-aaaa"
    assert {f["id"] for f in report["findings"]} == {
        "GHSA-test-0001-aaaa",
        "TEST-DEV-1",
        "TEST-IGNORED-1",
        "TEST-EXPIRED-1",
    }
    assert report["withdrawn"] == ["TEST-WITHDRAWN-1 (lintkit 0.5.0)"]


def test_a_dev_only_advisory_fails_too(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The brief's rule: any advisory without a live ignore entry fails,
    runtime or dev. The dev one is labelled, with the CVSS vector standing
    in for a reviewed rating."""
    ignore = _ignores(
        tmp_path,
        "\n".join(
            f'[[ignore]]\nid = "{vuln}"\nreason = "fixture"\nexpires = 2026-12-31\n'
            for vuln in ("GHSA-test-0001-aaaa", "TEST-IGNORED-1", "TEST-EXPIRED-1")
        ),
    )
    code, out, _err = _run(capsys, "--format", "json", "--ignore-file", str(ignore))
    assert code == 1
    report = json.loads(out)
    open_findings = [f for f in report["findings"] if f["status"] == "open"]
    assert [(f["id"], f["set"]) for f in open_findings] == [("TEST-DEV-1", "dev")]
    assert open_findings[0]["severity"].startswith("CVSS:3.1/")
    assert open_findings[0]["fixed"] == []


def test_every_advisory_accepted_passes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    ignore = _ignores(
        tmp_path,
        "\n".join(
            f'[[ignore]]\nid = "{vuln}"\nreason = "fixture"\nexpires = 2026-12-31\n'
            for vuln in (
                "CVE-0000-0001",
                "TEST-DEV-1",
                "TEST-IGNORED-1",
                "TEST-EXPIRED-1",
            )
        ),
    )
    code, out, _err = _run(capsys, "--ignore-file", str(ignore))
    assert code == 0
    assert "result: ok: 0 open advisories, 4 ignored, 0 expired ignore entries" in out
    assert "ignored: GHSA-test-0001-aaaa webframe 2.0.0" in out
    assert "ignore entry: CVE-0000-0001, expires 2026-12-31: fixture" in out


def test_an_ignored_hit_and_an_expired_ignore(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """cryptokit's entry holds; parser's has expired, so its advisory is
    open again and the expired entry is reported in its own right."""
    ignore = _ignores(
        tmp_path,
        '[[ignore]]\nid = "TEST-IGNORED-1"\nreason = "legacy mode never used"\n'
        "expires = 2026-12-31\n"
        '[[ignore]]\nid = "TEST-EXPIRED-1"\nreason = "waiting on 1.0.1"\n'
        "expires = 2026-10-01\n",
    )
    code, out, _err = _run(capsys, "--format", "json", "--ignore-file", str(ignore))
    assert code == 1
    report = json.loads(out)
    by_id = {f["id"]: f for f in report["findings"]}
    assert by_id["TEST-IGNORED-1"]["status"] == "ignored"
    assert by_id["TEST-IGNORED-1"]["ignore"]["reason"] == "legacy mode never used"
    assert by_id["TEST-EXPIRED-1"]["status"] == "expired"
    assert [e["id"] for e in report["ignore_entries"]["expired"]] == ["TEST-EXPIRED-1"]
    assert report["ignore_entries"]["unused"] == []


def test_an_expired_entry_fails_even_when_nothing_matches_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    ignore = _ignores(
        tmp_path,
        "\n".join(
            f'[[ignore]]\nid = "{vuln}"\nreason = "fixture"\nexpires = 2026-12-31\n'
            for vuln in (
                "GHSA-test-0001-aaaa",
                "TEST-DEV-1",
                "TEST-IGNORED-1",
                "TEST-EXPIRED-1",
            )
        )
        + '[[ignore]]\nid = "GHSA-gone-gone-gone"\nreason = "old"\nexpires = 2026-01-01\n',
    )
    code, out, _err = _run(capsys, "--ignore-file", str(ignore))
    assert code == 1
    assert "EXPIRED ignore entry: GHSA-gone-gone-gone (expired 2026-01-01)" in out
    assert "result: FAIL: 0 open advisories, 4 ignored, 1 expired ignore entries" in out


def test_an_unused_live_entry_is_a_note_not_a_failure(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    ignore = _ignores(
        tmp_path,
        "\n".join(
            f'[[ignore]]\nid = "{vuln}"\nreason = "fixture"\nexpires = 2026-12-31\n'
            for vuln in (
                "GHSA-test-0001-aaaa",
                "TEST-DEV-1",
                "TEST-IGNORED-1",
                "TEST-EXPIRED-1",
                "GHSA-gone-gone-gone",
            )
        ),
    )
    code, out, _err = _run(capsys, "--ignore-file", str(ignore))
    assert code == 0
    assert "note: ignore entry GHSA-gone-gone-gone matches no current advisory" in out


def test_an_entry_holds_through_its_date_and_not_a_day_longer() -> None:
    lock = _lock()
    entry = audit_lock.IgnoreEntry(id="TEST-IGNORED-1", reason="r", expires=TODAY)
    osv = audit_lock.OsvFixture(OSV)
    on_the_day = audit_lock.audit(lock, lock.root(), osv, [entry], today=TODAY)
    after = audit_lock.audit(
        lock, lock.root(), osv, [entry], today=TODAY + dt.timedelta(days=1)
    )
    status = {f.advisory.id: f.status for f in on_the_day.findings}
    assert status["TEST-IGNORED-1"] == "ignored"
    status = {f.advisory.id: f.status for f in after.findings}
    assert status["TEST-IGNORED-1"] == "expired"
    assert after.expired_entries == [entry]


def test_a_package_narrowed_entry_matches_only_that_package() -> None:
    lock = _lock()
    osv = audit_lock.OsvFixture(OSV)
    wrong = audit_lock.IgnoreEntry(
        id="TEST-IGNORED-1",
        reason="r",
        expires=dt.date(2026, 12, 31),
        package="webframe",
    )
    right = audit_lock.IgnoreEntry(
        id="TEST-IGNORED-1",
        reason="r",
        expires=dt.date(2026, 12, 31),
        package="CryptoKit",
    )
    status = {
        f.advisory.id: f.status
        for f in audit_lock.audit(lock, lock.root(), osv, [wrong], today=TODAY).findings
    }
    assert status["TEST-IGNORED-1"] == "open"
    status = {
        f.advisory.id: f.status
        for f in audit_lock.audit(lock, lock.root(), osv, [right], today=TODAY).findings
    }
    assert status["TEST-IGNORED-1"] == "ignored"


def test_scope_runtime_asks_only_about_the_runtime_set(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, _err = _run(
        capsys,
        "--scope",
        "runtime",
        "--format",
        "json",
        "--ignore-file",
        str(tmp_path / "none.toml"),
    )
    assert code == 1
    report = json.loads(out)
    assert report["packages"] == {"runtime": 5}
    assert "TEST-DEV-1" not in {f["id"] for f in report["findings"]}
    assert report["withdrawn"] == []


def test_the_text_report_names_each_finding(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, _err = _run(capsys, "--ignore-file", str(tmp_path / "none.toml"))
    assert code == 1
    assert out.startswith("audit_lock: 10 packages from ")
    assert "(5 runtime, 5 dev), scope all, source offline fixture" in out
    assert "not audited (not a PyPI package): vendored 0.1.0 (git " in out
    assert "VULNERABLE: GHSA-test-0001-aaaa webframe 2.0.0 [runtime, HIGH]" in out
    assert "  webframe reads an unbounded multipart body into memory" in out
    assert "  aliases: CVE-0000-0001, PYSEC-0000-1" in out
    assert "  fixed in: 2.0.1, 3.0.2" in out
    assert "  fixed in: no fixed version listed" in out
    assert "note: withdrawn advisory not reported: TEST-WITHDRAWN-1" in out
    assert out.endswith(
        "result: FAIL: 4 open advisories, 0 ignored, 0 expired ignore entries\n"
    )
    # Open runtime findings come first, ignored ones last.
    assert out.index("GHSA-test-0001-aaaa") < out.index("TEST-DEV-1")


def test_a_clean_lock_passes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fixture = tmp_path / "clean.json"
    fixture.write_text('{"results": {}, "vulns": {}}', encoding="utf-8")
    code = audit_lock.main(
        [
            "--lock",
            str(LOCK),
            "--offline",
            str(fixture),
            "--ignore-file",
            str(tmp_path / "none.toml"),
        ],
        today=TODAY,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.endswith(
        "result: ok: 0 open advisories, 0 ignored, 0 expired ignore entries\n"
    )


def test_an_audit_that_cannot_run_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Never a clean result from an audit that did not happen: a missing
    lock, a half-written ignore entry, an unreadable answer source."""
    bad = _ignores(tmp_path, '[[ignore]]\nid = "X-1"\nexpires = 2026-12-31\n')
    none = str(tmp_path / "none.toml")
    missing = str(tmp_path / "missing")
    cases = [
        (["--lock", missing, "--offline", str(OSV), "--ignore-file", none], "missing"),
        (
            ["--lock", str(LOCK), "--offline", str(OSV), "--ignore-file", str(bad)],
            "no reason",
        ),
        (["--lock", str(LOCK), "--offline", missing, "--ignore-file", none], "missing"),
    ]
    for argv, message in cases:
        assert audit_lock.main(argv, today=TODAY) == 2
        err = capsys.readouterr().err
        assert err.startswith("audit_lock: cannot audit: ")
        assert message in err


def test_a_fixture_without_a_named_vulnerability_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fixture = tmp_path / "partial.json"
    fixture.write_text(
        json.dumps({"results": {"webframe@2.0.0": {"vulns": [{"id": "NOPE-1"}]}}}),
        encoding="utf-8",
    )
    code = audit_lock.main(
        [
            "--lock",
            str(LOCK),
            "--offline",
            str(fixture),
            "--ignore-file",
            str(tmp_path / "n"),
        ],
        today=TODAY,
    )
    assert code == 2
    assert "no recorded vulnerability NOPE-1" in capsys.readouterr().err


# ----------------------------------------------------------- OSV itself


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _Opener:
    """urllib.request.urlopen's stand-in: answers in order, records the
    requests."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> _Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _Response(answer)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.osv.dev/x", code, "status", email.message.Message(), io.BytesIO()
    )


def test_the_client_posts_one_query_per_package() -> None:
    opener = _Opener({"results": [{}, {"vulns": [{"id": "GHSA-1"}]}]})
    client = audit_lock.OsvClient(opener=opener, sleep=lambda _s: None)
    queries = [
        {"package": {"name": "a", "ecosystem": "PyPI"}, "version": "1"},
        {"package": {"name": "b", "ecosystem": "PyPI"}, "version": "2"},
    ]
    assert client.querybatch(queries) == [{}, {"vulns": [{"id": "GHSA-1"}]}]
    request = opener.requests[0]
    assert request.full_url == "https://api.osv.dev/v1/querybatch"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"queries": queries}
    assert request.get_header("User-agent").startswith("bettermemory-audit-lock")


def test_the_client_reads_one_vulnerability_by_quoted_id() -> None:
    opener = _Opener({"id": "GHSA-a/b"})
    client = audit_lock.OsvClient(opener=opener, sleep=lambda _s: None)
    assert client.vuln("GHSA-a/b") == {"id": "GHSA-a/b"}
    assert opener.requests[0].full_url == "https://api.osv.dev/v1/vulns/GHSA-a%2Fb"
    assert opener.requests[0].get_method() == "GET"


def test_the_client_retries_a_server_error_then_succeeds() -> None:
    waits: list[float] = []
    opener = _Opener(
        _http_error(503), urllib.error.URLError("reset"), {"results": [{}]}
    )
    client = audit_lock.OsvClient(opener=opener, sleep=waits.append, backoff=2.0)
    assert client.querybatch([{"package": {"name": "a"}, "version": "1"}]) == [{}]
    assert waits == [2.0, 4.0]


def test_the_client_does_not_retry_a_client_error() -> None:
    opener = _Opener(_http_error(400))
    client = audit_lock.OsvClient(opener=opener, sleep=lambda _s: None)
    with pytest.raises(audit_lock.AuditError, match="HTTP 400"):
        client.vuln("X-1")
    assert len(opener.requests) == 1


def test_the_client_gives_up_loudly() -> None:
    opener = _Opener(TimeoutError("slow"), TimeoutError("slow"), TimeoutError("slow"))
    client = audit_lock.OsvClient(opener=opener, sleep=lambda _s: None)
    with pytest.raises(audit_lock.AuditError, match="slow"):
        client.querybatch([{"package": {"name": "a"}, "version": "1"}])
    assert len(opener.requests) == 3


def test_an_answer_of_the_wrong_length_is_refused() -> None:
    opener = _Opener({"results": []})
    client = audit_lock.OsvClient(opener=opener, sleep=lambda _s: None)
    with pytest.raises(audit_lock.AuditError, match="one result per query"):
        client.querybatch([{"package": {"name": "a"}, "version": "1"}])


class _PagedOsv:
    """Two pages for package a, and a batch limit of two queries."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def querybatch(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls.append(queries)
        out: list[dict[str, Any]] = []
        for query in queries:
            name = query["package"]["name"]
            if name == "a" and "page_token" not in query:
                out.append({"vulns": [{"id": "A-1"}], "next_page_token": "t2"})
            elif name == "a":
                out.append({"vulns": [{"id": "A-2"}, {"id": "A-1"}]})
            else:
                out.append({})
        return out

    def vuln(self, vuln_id: str) -> dict[str, Any]:
        return {"id": vuln_id}


def test_queries_are_batched_and_pages_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_lock, "BATCH_LIMIT", 2)
    packages = [audit_lock.Locked(name=n, version="1", set="runtime") for n in "abc"]
    osv = _PagedOsv()
    found = audit_lock.query(osv, packages)
    assert found == {packages[0]: ["A-1", "A-2"], packages[1]: [], packages[2]: []}
    # The follow-up page rides in the next batch beside c.
    assert [len(call) for call in osv.calls] == [2, 2]
    assert osv.calls[1] == [
        {"package": {"name": "c", "ecosystem": "PyPI"}, "version": "1"},
        {
            "package": {"name": "a", "ecosystem": "PyPI"},
            "version": "1",
            "page_token": "t2",
        },
    ]


def test_fold_keeps_separate_vulnerabilities_apart() -> None:
    docs = [
        {"id": "PYSEC-1", "aliases": ["CVE-1"]},
        {"id": "GHSA-2", "aliases": ["CVE-2"], "summary": "two"},
        {"id": "GHSA-1", "aliases": ["CVE-1", "PYSEC-1"], "summary": "one"},
        {"id": "OSV-3", "details": "  three\n spread over\nlines  "},
    ]
    folded = audit_lock.fold(docs, "x")
    assert [(a.id, a.aliases, a.summary) for a in folded] == [
        ("GHSA-1", ("CVE-1", "PYSEC-1"), "one"),
        ("GHSA-2", ("CVE-2",), "two"),
        ("OSV-3", (), "three spread over lines"),
    ]


# ------------------------------------------------------- the workflow


def test_the_audit_workflow_runs_both_checks_and_gates_no_release() -> None:
    """audit.yml runs this script and the license check. None of the
    scanning workflows can be called as a reusable workflow, and release.yml
    calls none of them, so none can block a tag."""
    import yaml

    workflows = REPO / ".github" / "workflows"
    scanning = ("audit.yml", "codeql.yml", "scorecard.yml")
    loaded = {
        name: yaml.safe_load((workflows / name).read_text("utf-8")) for name in scanning
    }
    runs = [
        step.get("run", "")
        for job in loaded["audit.yml"]["jobs"].values()
        for step in job["steps"]
        if isinstance(step, dict)
    ]
    assert any(r.startswith("python3 tools/audit_lock.py") for r in runs)
    assert any("tools/sbom.py --licenses" in r for r in runs)
    # PyYAML reads the bare `on:` key as True.
    triggers = loaded["audit.yml"][True]
    assert "uv.lock" in triggers["pull_request"]["paths"]
    assert "schedule" in triggers
    for name, workflow in loaded.items():
        assert "workflow_call" not in workflow[True], name
    release = yaml.safe_load((workflows / "release.yml").read_text("utf-8"))
    called = {str(job.get("uses", "")) for job in release["jobs"].values()}
    assert not {u for u in called if any(name in u for name in scanning)}


@pytest.mark.skipif(
    os.environ.get("BETTERMEMORY_TEST_NETWORK") != "1",
    reason="asks the real OSV; set BETTERMEMORY_TEST_NETWORK=1",
)
def test_the_real_lock_against_the_real_osv(capsys: pytest.CaptureFixture[str]) -> None:
    code = audit_lock.main(["--lock", str(REPO / "uv.lock"), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert code in (0, 1)
    assert report["source"] == "https://api.osv.dev"
    assert (
        sum(report["packages"].values())
        == len(uvlock.load(REPO / "uv.lock").packages) - 1
    )
