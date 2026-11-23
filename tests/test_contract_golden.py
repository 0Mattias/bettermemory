"""The contract golden files (tests/contract/golden) match the Python
reference byte for byte.

Each generator in `tests.contract.generate` runs in memory and its output
is compared with the committed files. A generator whose output reads the
Unicode database, the linked SQLite or the linked zlib is pinned to the
versions the files were made under: under those it must match; under
others a match passes and a difference skips, naming the drift, since the
files then describe the pinned versions and not this interpreter's.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import tempfile
import unicodedata
import warnings
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.contract._diff import _differing
from tests.contract._sandbox import Scrubber
from tests.contract.kinds import call_sites, writable_kinds
from tests.contract.payloads import LEGACY_SHAPES
from tests.contract.generate import (
    GENERATORS,
    GOLDEN,
    LeftStateError,
    PYDANTIC_CORE_VERSION,
    PYDANTIC_GATED,
    PYTHON_GATED,
    PLATFORM,
    PLATFORM_GATED,
    POSIX_ONLY,
    PYTHON_VERSION,
    SQLITE_GATED,
    SQLITE_VERSION,
    UNICODE_GATED,
    UNIDATA_VERSION,
    ZLIB_GATED,
    ZLIB_VERSION,
    platform_key,
    pydantic_core_version,
    python_version,
    run_generator,
    zlib_version,
)

#: The tool-response fixture records the review the package ships
#: (`review.TAU_UNSAFE`, `TAU_SECRET`), which the suite turns off by default.
pytestmark = pytest.mark.shipped_tau


@pytest.fixture(autouse=True)
def _scratch_under_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The generators' scratch directories (stores, repositories) live
    under the test's own directory, where the suite allows a location
    capture."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _drift(name: str) -> list[str]:
    out = []
    if name in UNICODE_GATED and unicodedata.unidata_version != UNIDATA_VERSION:
        out.append(f"Unicode {unicodedata.unidata_version} (files: {UNIDATA_VERSION})")
    if name in SQLITE_GATED and sqlite3.sqlite_version != SQLITE_VERSION:
        out.append(f"SQLite {sqlite3.sqlite_version} (files: {SQLITE_VERSION})")
    if name in PYTHON_GATED and python_version() != PYTHON_VERSION:
        out.append(f"Python {python_version()} (files: {PYTHON_VERSION})")
    if name in ZLIB_GATED and zlib_version() != ZLIB_VERSION:
        out.append(f"zlib {zlib_version()} (files: {ZLIB_VERSION})")
    if name in PYDANTIC_GATED and pydantic_core_version() != PYDANTIC_CORE_VERSION:
        out.append(
            f"pydantic-core {pydantic_core_version()} (files: {PYDANTIC_CORE_VERSION})"
        )
    if name in PLATFORM_GATED and platform_key() != PLATFORM:
        out.append(f"platform {platform_key()} (files: {PLATFORM})")
    return out


#: Why a ``POSIX_ONLY`` generator is not run on Windows.
_NOT_POSIX = "POSIX only: the generator has no case on Windows"


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_golden_files_match_the_reference(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    try:
        _compare_with_the_reference(name)
    finally:
        _no_lone_surrogate_logged(name, caplog.records)


def _no_lone_surrogate_logged(name: str, records: list[logging.LogRecord]) -> None:
    """A generator that captures a logger's records keeps them from
    propagating. A record left in pytest's capture with a lone surrogate
    makes the test report impossible to send from an xdist worker, which
    kills the worker; this fails the same fault in a single process."""
    for record in records:
        message = record.getMessage()
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in message):
            pytest.fail(
                f"{name} left a log record with a lone surrogate in pytest's "
                f"capture (logger {record.name!r}): {ascii(message)}; capture "
                "it with tests.contract._common.captured_by so it does not "
                "propagate",
                pytrace=False,
            )


#: A JSON string literal on one line of a golden file.
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


def _lone_surrogate_at(data: bytes) -> int | None:
    """The first line of a ``.json`` or ``.jsonl`` file holding a JSON
    string (a key or a value, at any depth) with a lone surrogate, or None.
    A plain JSON string cannot carry one to a strict reader (serde_json
    refuses ``"\\udcc3"``), so the native tests could not read the file
    at all, not even to set its differing rows aside under an open gate."""
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        for literal in _STRING.findall(line):
            text = json.loads(literal)
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
                return number
    return None


#: Files that hold lone surrogates by design, each read by its native test
#: through the port's own `json::loads` (Python's reader, which takes a
#: lone surrogate), never through serde_json: the test file named.
SURROGATE_READERS: dict[str, str] = {
    "capture_vocabulary.jsonl": "golden_capture_vocabulary.rs",
    "identity_caller.jsonl": "golden_identity_caller.rs",
    "membership_records_records.jsonl": "golden_membership_records_records.rs",
}

_NATIVE_TESTS = (
    Path(__file__).resolve().parent.parent
    / "native"
    / "crates"
    / "bettermemory-core"
    / "tests"
)


def test_the_surrogate_readers_read_through_the_port() -> None:
    for filename, test in SURROGATE_READERS.items():
        source = (_NATIVE_TESTS / test).read_text(encoding="utf-8")
        assert filename in source, (filename, test)
        assert "json::loads(&unicode::cps(" in source, (filename, test)


def _no_lone_surrogate_written(name: str, produced: dict[str, bytes]) -> None:
    for filename, data in sorted(produced.items()):
        if not filename.endswith((".json", ".jsonl")):
            continue
        if filename in SURROGATE_READERS:
            continue
        line = _lone_surrogate_at(data)
        if line is not None:
            pytest.fail(
                f"generator {name} wrote a lone surrogate into {filename} at "
                f"line {line} under Python {python_version()}; write the text "
                "through tests.contract._common.text_field (a ``_cps`` code "
                "point list when it holds a surrogate)",
                pytrace=False,
            )


#: Golden files outside ``PYTHON_GATED`` that may hold RecursionError: the
#: number of lines holding it, and why the answer is the same under every
#: interpreter the files are compared under. Any other such file fails
#: `_no_recursion_row_ungated`: a raise at the depth where `json.loads` or
#: the C stack stops is the interpreter's and the system's (3.14 on Linux
#: reads 100,000 nested arrays that 3.11 to 3.13 refuse), so its rows go
#: in the unit's Python-gated ``<name>_python`` sibling.
RECURSION_ROWS: dict[str, tuple[int, str]] = {
    "membership_core.jsonl": (
        1,
        "`Membership._event` passes the event's payload, 1,000 lists deep, "
        "through `membership_rows._jsonable`, a Python function that calls "
        "itself per level: the raise is Python's recursion limit of 1,000 "
        "frames, which counts Python frames alike on every release and "
        "system (gen_membership_core._deep_event)",
    ),
}


def _no_recursion_row_ungated(name: str, produced: dict[str, bytes]) -> None:
    """No committed file of a generator outside ``PYTHON_GATED`` holds a
    RecursionError row unless `RECURSION_ROWS` lists it with its count."""
    if name in PYTHON_GATED:
        return
    for filename in sorted(produced):
        path = GOLDEN / filename
        if not path.is_file() or b"RecursionError" not in path.read_bytes():
            continue
        lines = path.read_bytes().splitlines()
        count = sum(1 for line in lines if b"RecursionError" in line)
        listed = RECURSION_ROWS.get(filename)
        assert listed is not None and listed[0] == count, (
            f"{filename} (generator {name}, not Python gated) holds {count} "
            "lines with RecursionError, where the interpreter's recursion "
            "budget decides the answer: move those rows to a Python-gated "
            "sibling <name>_python.jsonl, or list the file in "
            "RECURSION_ROWS with the count and why every interpreter and "
            "system answers alike"
        )


def test_the_recursion_table_is_current() -> None:
    """Each `RECURSION_ROWS` entry whose file exists names its count of
    RecursionError lines and belongs to no Python-gated generator."""
    for filename, (count, reason) in RECURSION_ROWS.items():
        assert reason
        path = GOLDEN / filename
        if not path.is_file():
            continue
        lines = path.read_bytes().splitlines()
        assert sum(1 for line in lines if b"RecursionError" in line) == count, filename
        assert filename.rsplit(".", 1)[0] not in PYTHON_GATED, filename


def _compare_with_the_reference(name: str) -> None:
    if name in POSIX_ONLY and sys.platform == "win32":
        pytest.skip(f"{name}: {_NOT_POSIX}")
    drift = _drift(name)
    try:
        produced = run_generator(name)
    except LeftStateError:
        # A leak fails under every interpreter, whatever the gates.
        raise
    except Exception as exc:
        # Under the pinned versions a generator that raises is a failure.
        # Under others it has met something those versions do otherwise,
        # which is the difference the gate exists to skip.
        if not drift:
            raise
        reason = f"{name} raised {type(exc).__name__}: {exc} under {', '.join(drift)}"
        warnings.warn(reason, stacklevel=1)
        pytest.skip(reason)
    assert produced, name
    # Both guards run before any skip for drift: they hold under every
    # interpreter, whatever the gates.
    _no_lone_surrogate_written(name, produced)
    _no_recursion_row_ungated(name, produced)
    for filename, data in sorted(produced.items()):
        committed = (GOLDEN / filename).read_bytes()
        if data != committed and drift:
            where = _differing(data, committed)
            # A warning as well as the skip reason: a quiet run prints the
            # warnings summary and not the reasons, and which records
            # another version changes is worth reading in a CI log.
            warnings.warn(
                f"{filename} differs under {', '.join(drift)}: {where}", stacklevel=1
            )
            pytest.skip(f"{filename} differs under {', '.join(drift)}: {where}")
        assert data == committed, (
            f"{filename} no longer matches the reference: {_differing(data, committed)}"
        )


def test_every_golden_file_has_a_generator() -> None:
    made: set[str] = set()
    unknown = False
    for name, generate in GENERATORS.items():
        if name in POSIX_ONLY and sys.platform == "win32":
            # Its files are on disk and not made here.
            unknown = True
            continue
        try:
            made |= set(run_generator(name, generate))
        except LeftStateError:
            raise
        except Exception:
            # A gated generator that raises under another version than its
            # files' names no files here; under the pinned ones it fails.
            if not _drift(name):
                raise
            unknown = True
    on_disk = {p.name for p in GOLDEN.iterdir() if p.is_file()}
    if unknown:
        assert made <= on_disk
    else:
        assert on_disk == made


def test_real_store_dump_masks_a_mac_containing_an_id_shaped_digit_run() -> None:
    from tests.contract.gen_store_files import _masked

    digits = "12345678901234567890123456"
    mac = "ab" * 18 + digits + "cd"
    assert len(mac) == 64
    statement = f"VALUES ('{digits}', '{mac}', 'kept')"
    dumped = {
        "page_size": 4096,
        "journal_mode": "wal",
        "statements": [statement],
        "keys": {},
    }
    assert _masked(dumped)["statements"] == ["VALUES ('<ulid>', '<hex>', 'kept')"]


def test_real_store_dump_preserves_the_log_chain_anchor() -> None:
    from bettermemory.log import GENESIS_MAC
    from tests.contract.gen_store_files import _masked

    def masked_anchor(value: str) -> object:
        statement = f"INSERT INTO log (prev_mac, mac) VALUES ('{value}', '{'ab' * 32}')"
        return _masked(
            {
                "page_size": 4096,
                "journal_mode": "wal",
                "statements": [statement],
                "keys": {},
            }
        )["statements"]

    assert masked_anchor(GENESIS_MAC) != masked_anchor("01" * 32)


def test_no_golden_file_carries_a_host_fact() -> None:
    from tests.contract.gen_store_files import host_fact

    leaks = {
        path.name: leak
        for path in sorted(GOLDEN.iterdir())
        if path.is_file() and (leak := host_fact(path.read_bytes()))
    }
    assert leaks == {}


@pytest.mark.parametrize(
    ("data", "caught"),
    [
        (rb'"/tmp/a b"', False),
        (rb'"/tmp/a bc"', True),
        (rb'"/tmp/x y/zz"', True),
        (rb'"/home/fixture/other"', True),
        (rb'"C:\\Users\\example\\file.txt"', False),
        (rb'"C:\\Users\\example\\file.txt.bak"', True),
        (rb"C:\Users\x", True),
        (rb"\\?\C:\x", True),
        (rb"/Volumes/Data/x", True),
        (rb"D:/Users/x", True),
        (b"\x00/private/var\xff", True),
    ],
)
def test_the_host_fact_guard(data: bytes, caught: bool) -> None:
    from tests.contract.gen_store_files import host_fact

    assert (host_fact(data) is not None) is caught


def _nested(text: str, depth: int) -> bytes:
    """`text` as a JSON string, inside a JSON string `depth` - 1 times."""
    for _ in range(depth):
        text = json.dumps(text)
    return text.encode()


@pytest.mark.parametrize(
    ("data", "hit"),
    [
        # macOS's text for errno 88, exempt as exactly that text, in any
        # data.
        (b'"cwd unreachable: Malformed Mach-o file"', None),
        (b'{"detail":"cwd unreachable: Malformed Mach-o file"}\x00', None),
        (b'{"detail":"cwd unreachable: Malformed Mach-o file"}\xff', None),
        # A name beside it is caught, and so is the text spelled otherwise.
        (b'"cwd unreachable: Malformed Mach-o file on Mac"', "Mac"),
        (b'"fixtureuser: Malformed Mach-o file"', "fixtureuser"),
        (b'"MacMalformed Mach-o file"', "Mac"),
        (b"\x00Malformed Mach-o file\x00Mac", "Mac"),
        (b'"Malformed Mach-O file"', "Mac"),
        (b'"Malformed Mach-o\\nfile"', "Mac"),
        # Inside a longer word.
        (b"Macintosh", "Mac"),
        (b"iMac", "Mac"),
        (b"Mac_1", "Mac"),
        (b"Mac9", "Mac"),
        (b"fixtureusers", "fixtureuser"),
        (b"xfixtureuser", "fixtureuser"),
        (b"fixtureuser_2", "fixtureuser"),
        (b"\x00fixtureusers", "fixtureuser"),
        # Standing alone: the whole text, a value, a word in prose.
        (b"Mac", "Mac"),
        (b'{"hostname":"=Mac"}', "Mac"),
        (b"runs on Mac today", "Mac"),
        (b"fixtureuser", "fixtureuser"),
        # In a path.
        (b'"/srv/Mac/x"', "Mac"),
        (b'"/srv/fixtureuser/notes"', "fixtureuser"),
        (b'"D:\\\\fixtureuser\\\\x"', "fixtureuser"),
        # Backslashes that start no JSON escape, in a text that is not
        # JSON: as written, where read `\f` is a form feed.
        (b"D:\\srv\\Mac\\x", "Mac"),
        (b"D:\\srv\\fixtureuser", "fixtureuser"),
        # After a separator, or before one.
        (b"node-Mac", "Mac"),
        (b"Mac.local", "Mac"),
        (b"by fixtureuser@Mac", "fixtureuser"),
        (b"a,Mac,b", "Mac"),
        # Beside a JSON escape, in a JSON text or one nested in another.
        (b'"x\\nMac"', "Mac"),
        (b'"x\\tfixtureuser"', "fixtureuser"),
        (b'"\\u2014Mac"', "Mac"),
        (b'"\\u00e9Mac"', "Mac"),
        (b'"Mac\\u00e9"', "Mac"),
        (b'"fixtureuser\\u00e9"', "fixtureuser"),
        (_nested("line\nfixtureuser", 2), "fixtureuser"),
        (_nested("a\tMac", 2), "Mac"),
        (_nested("a\r\nfixtureuser", 2), "fixtureuser"),
        (_nested("x\u2014Mac", 2), "Mac"),
        (_nested("\U0001f600fixtureuser", 2), "fixtureuser"),
        (b'"\\\\u002ffixtureuser"', "fixtureuser"),
        # Spelled with JSON escapes, one level deep or two, in either case.
        (b'"\\u004dac"', "Mac"),
        (b'"\\\\u004dac"', "Mac"),
        (b'"\\u004Dac"', "Mac"),
        # Down the fixture world's HOME, whose /home/ alone is no host path.
        (b'"<scratch>/home/fixtureuser/x"', "fixtureuser"),
        (b'"<scratch>/home/x/Mac"', "Mac"),
        # After an encoding that ends in a word character: percent-encoding,
        # terminal colour, a Python escape, as text or in JSON.
        (b"https://e.com/?p=%2FUsers%2Ffixtureuser%2Fx", "fixtureuser"),
        (b"file:///C%3A%5CUsers%5Cfixtureuser", "fixtureuser"),
        (b"https://e.com/%7Efixtureuser/", "fixtureuser"),
        (b"q=on%20Mac%20today", "Mac"),
        (_nested("\x1b[1mfixtureuser\x1b[0m", 1), "fixtureuser"),
        (b"\x1b[1mMac\x1b[0m", "Mac"),
        (json.dumps({"b": ["", "x1bMac"]}).encode(), "Mac"),
        (b"b'\\x00fixtureuser'", "fixtureuser"),
    ],
)
def test_the_host_fact_guard_reads_a_name_wherever_it_stands(
    monkeypatch: pytest.MonkeyPatch, data: bytes, hit: str | None
) -> None:
    import getpass
    import socket

    from tests.contract.gen_store_files import host_fact

    monkeypatch.setattr(getpass, "getuser", lambda: "fixtureuser")
    monkeypatch.setattr(socket, "gethostname", lambda: "Mac.local")
    assert host_fact(data) == hit


@pytest.mark.parametrize(
    "name",
    [
        "fixtureus\u00e9r",
        # Past U+FFFF: written as a surrogate pair, two escapes.
        "fixtureus\U0001d4ear",
    ],
)
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_the_host_fact_guard_reads_a_name_written_with_json_escapes(
    monkeypatch: pytest.MonkeyPatch, name: str, depth: int
) -> None:
    """The fixtures are ASCII JSON, so a name holding a character outside
    ASCII is written as an escape, one level deeper for each JSON text it
    is nested in. A third level's escape starts with four backslashes,
    read as a UNC prefix before a name is looked for."""
    import getpass
    import socket

    from tests.contract.gen_store_files import host_fact

    monkeypatch.setattr(getpass, "getuser", lambda: name)
    monkeypatch.setattr(socket, "gethostname", lambda: "Mac.local")
    data = _nested(f"/srv/{name}/x", depth)
    assert name.encode() not in data
    assert host_fact(data) == (name if depth < 3 else "\\\\\\\\")


@pytest.mark.parametrize(
    ("name", "before", "after"),
    [
        # A POSIX user name whose first byte is no UTF-8, as getuser reads it
        # (surrogateescape): a lone low half first, a high half before it.
        (b"\x80fixture".decode("utf-8", "surrogateescape"), "\ud83d", "/x"),
        # A lone high half last, a low half after it.
        ("fixture\ud83d", "/srv/", "\udc80"),
    ],
)
@pytest.mark.parametrize("depth", [1, 2])
def test_the_host_fact_guard_reads_a_lone_half_beside_the_other(
    monkeypatch: pytest.MonkeyPatch, name: str, before: str, after: str, depth: int
) -> None:
    """A name holding a lone surrogate half, written with JSON escapes
    beside an escape of the other half, is read with the halves apart: the
    two joined spell one character past U+FFFF that no name holds."""
    import getpass
    import socket

    from tests.contract.gen_store_files import host_fact

    monkeypatch.setattr(getpass, "getuser", lambda: name)
    monkeypatch.setattr(socket, "gethostname", lambda: "xyzhost.local")
    data = _nested(before + name + after, depth)
    if depth == 1 and before == "\ud83d":
        assert data == b'"\\ud83d\\udc80fixture/x"'
    assert host_fact(data) == name


def test_the_host_fact_guard_reads_a_surrogate_pair_apart_and_joined() -> None:
    """A surrogate pair's two escapes read as its two halves, the next level
    read from them, then joined into the one character they spell, where
    that differs; a lone half stays as it is."""
    from tests.contract.gen_store_files import (
        _json_readings,
        _json_unescaped,
        _paired,
    )

    high, low = chr(0xD835), chr(0xDCEA)
    apart = f"a{high}{low}b"
    assert _json_unescaped("a\\ud835\\udceab") == apart
    assert _json_unescaped("a\\uD835\\uDCEAb") == apart
    assert _paired(apart) == "a\U0001d4eab"
    assert _paired(f"a{high}b{low}") == f"a{high}b{low}"
    assert _paired(f"a{low}{high}b") == f"a{low}{high}b"
    assert _json_readings("\\\\ud835\\\\udcea") == [
        "\\\\ud835\\\\udcea",
        "\\ud835\\udcea",
        high + low,
        "\U0001d4ea",
    ]
    assert _json_readings("\\\\ud835x") == ["\\\\ud835x", "\\ud835x", high + "x"]


def test_the_host_fact_guard_reads_json_escapes_level_by_level() -> None:
    """Each level of JSON escapes is read while one changes the text, up to
    `_JSON_DEPTH` levels, eight."""
    from tests.contract.gen_store_files import _JSON_DEPTH, _json_readings

    assert _JSON_DEPTH == 8
    assert _json_readings("/srv/x") == ["/srv/x"]
    assert _json_readings('"a\\\\nb"') == ['"a\\\\nb"', '"a\\nb"', '"a\nb"']
    readings = _json_readings(_nested("\u00e9", _JSON_DEPTH).decode())
    assert len(readings) == _JSON_DEPTH + 1
    assert "\u00e9" in readings[-1]
    assert "\u00e9" not in readings[-2]
    deeper = _json_readings(_nested("\u00e9", _JSON_DEPTH + 1).decode())
    assert len(deeper) == _JSON_DEPTH + 1
    assert not any("\u00e9" in r for r in deeper)


def test_the_host_fact_guard_reads_no_short_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name under three characters is no needle, standing alone too."""
    import getpass
    import socket

    from tests.contract.gen_store_files import host_fact

    monkeypatch.setattr(getpass, "getuser", lambda: "ab")
    monkeypatch.setattr(socket, "gethostname", lambda: "xy.local")
    assert host_fact(b'ab xy "/srv/ab/x" xy.local') is None


@pytest.mark.parametrize(
    ("columns", "hit"),
    [
        # One text, the name at its start or at its end.
        (("fixtureuserabc",), "fixtureuser"),
        (("xyzMac",), "Mac"),
        # Two texts, which the record stores back to back.
        (("fixtureuser", "abc"), "fixtureuser"),
        (("xyz", "Mac"), "Mac"),
        # Neither name: the file itself holds none.
        (("abc", "xyz"), None),
    ],
)
def test_the_host_fact_guard_reads_names_in_an_sqlite_file_as_substrings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    columns: tuple[str, ...],
    hit: str | None,
) -> None:
    """An SQLite file stores a record's column texts back to back, so a
    name in it counts wherever it stands, inside a longer word too."""
    import getpass
    import socket
    from contextlib import closing

    from tests.contract.gen_store_files import host_fact

    monkeypatch.setattr(getpass, "getuser", lambda: "fixtureuser")
    monkeypatch.setattr(socket, "gethostname", lambda: "Mac.local")
    path = tmp_path / "rows.sqlite"
    names = ", ".join(f"c{i} TEXT" for i in range(len(columns)))
    marks = ", ".join("?" for _ in columns)
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"CREATE TABLE t ({names})")
        db.execute(f"INSERT INTO t VALUES ({marks})", columns)
        db.commit()
    assert host_fact(path.read_bytes()) == hit


def test_the_errno_fixture_passes_the_host_fact_guard_on_a_host_named_mac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``location_probe_errno.jsonl`` holds macOS's text for errno 88,
    "Malformed Mach-o file", which the guard exempts where that exact text
    stands alone (`PLATFORM_TEXTS`)."""
    import getpass
    import socket

    from tests.contract.gen_store_files import host_fact

    monkeypatch.setattr(getpass, "getuser", lambda: "fixtureuser")
    monkeypatch.setattr(socket, "gethostname", lambda: "Mac.local")
    data = (GOLDEN / "location_probe_errno.jsonl").read_bytes()
    assert b"Malformed Mach-o file" in data
    assert host_fact(data) is None


@pytest.mark.parametrize(
    ("user", "host", "data", "hit"),
    [
        # Standing alone in a JSON row: exempt, on a host named "Mac" or one
        # whose name the text's start ends.
        ("fixtureuser", "Mac", b'{"cwd":"cwd: Malformed Mach-o file"}\n', None),
        ("fixtureuser", "xMal", b'{"cwd":"cwd: Malformed Mach-o file"}\n', None),
        # A name running into it from either side: the text no longer
        # stands alone and is read with the name.
        ("fixtureuser", "xMal", b'{"cwd":"cwd: xMalformed Mach-o file"}', "xMal"),
        ("ilety", "xyzhost", b'{"cwd":"cwd: Malformed Mach-o filety"}', "ilety"),
        # A name sharing characters with the text and running past it
        # across a character that is no word character.
        ("fixtureuser", "file-srv", b"err: Malformed Mach-o file-srv x", "file-srv"),
        ("fixtureuser", "srv-Mal", b"srv-Malformed Mach-o file", "srv-Mal"),
        ("file.x", "xyzhost", b'"Malformed Mach-o file.x"', "file.x"),
        ("ach-o f", "xyzhost", b'"Malformed Mach-o file"', None),
        # The same with the name's first character escaped.
        ("fixtureuser", "xMal", b'"cwd: \\u0078Malformed Mach-o file"', "xMal"),
        # Beside a word character outside ASCII.
        ("fixtureuser", "Mac", '"\u00e9Malformed Mach-o file"'.encode(), "Mac"),
        ("fixtureuser", "Mac", '"Malformed Mach-o file\u00e9"'.encode(), "Mac"),
        # A drive written straight after it, on a host whose name the text
        # does not hold: the text does not stand alone, and the drive's
        # letter follows the "e" of "file".
        (
            "fixtureuser",
            "xyzhost",
            b'"Malformed Mach-o fileD:/Documents/x"',
            "D:/Documents",
        ),
        (
            "fixtureuser",
            "xyzhost",
            rb"Malformed Mach-o fileC:\Windows\x",
            "C:\\Windows",
        ),
        (
            "fixtureuser",
            "xyzhost",
            rb'{"e":"Malformed Mach-o fileE:\\Documents\\x"}',
            "E:\\\\Documents",
        ),
    ],
)
def test_the_host_fact_guard_exempts_a_platform_text_only_standing_alone(
    monkeypatch: pytest.MonkeyPatch, user: str, host: str, data: bytes, hit: str | None
) -> None:
    import getpass
    import socket

    from tests.contract.gen_store_files import host_fact

    monkeypatch.setattr(getpass, "getuser", lambda: user)
    monkeypatch.setattr(socket, "gethostname", lambda: f"{host}.local")
    assert host_fact(data) == hit


def test_the_host_fact_guard_replaces_a_synthetic_path_by_an_empty_string() -> None:
    """A synthetic path exactly as written is replaced by `""`, so the text
    on either side is not joined; a platform text is left as it stands
    (`_named` reads it); the fixture world's HOME loses its /home only."""
    from tests.contract.gen_store_files import _exempted

    row = '{"d":"cwd: Malformed Mach-o file","p":"/tmp/a b","q":"/tmp/a bc"}'
    assert _exempted(row) == '{"d":"cwd: Malformed Mach-o file","p":"","q":"/tmp/a bc"}'
    assert _exempted("<scratch>/home/x <scratch>/home") == "<scratch>/x <scratch>"
    assert _exempted("<scratch>/homes") == "<scratch>/homes"


@pytest.mark.parametrize(
    ("data", "hit"),
    [
        (b"/private/x", "/private"),
        (b"/var/folders/ab/T/x", "/var/folders"),
        (b"/Users/x", "/Users"),
        (b"/tmp/x", "/tmp"),
        (b"/home/x", "/home/"),
        (b"/Volumes/x", "/Volumes/"),
        (b"/root/x", "/root/"),
        (b"x/claude-501/y", "claude-501"),
        (rb"\\?\C:\x", "\\\\?\\"),
        (rb"\\\\srv\\x", "\\\\\\\\"),
        (rb"C:\Users", ":\\Users"),
        (rb"C:\\Users", ":\\\\Users"),
        (b"x/AppData/y", "AppData"),
        # A drive's user, system or documents folder.
        (rb"C:\Windows\x", "C:\\Windows"),
        (b"D:/Documents/x", "D:/Documents"),
        (rb"E:\\Documents\\x", "E:\\\\Documents"),
        # Its letter after a word character.
        (rb"xC:\Windows\x", "C:\\Windows"),
        (b"fileD:/Documents/x", "D:/Documents"),
        (rb"_E:\\Documents\\x", "E:\\\\Documents"),
    ],
)
def test_the_host_fact_guard_names_the_host_path_it_reads(
    monkeypatch: pytest.MonkeyPatch, data: bytes, hit: str
) -> None:
    import getpass
    import socket

    from tests.contract.gen_store_files import host_fact

    monkeypatch.setattr(getpass, "getuser", lambda: "fixtureuser")
    monkeypatch.setattr(socket, "gethostname", lambda: "Mac.local")
    assert host_fact(data) == hit


def _hex_row(answer: str | bytes, upper: bool = False) -> bytes:
    """A row shaped like fs_realpath.jsonl's, its answer's bytes as hex."""
    raw = answer.encode() if isinstance(answer, str) else answer
    spelled = raw.hex().upper() if upper else raw.hex()
    return json.dumps({"q": {"p": "=<root>/a"}, "r": spelled}).encode()


@pytest.mark.parametrize(
    ("answer", "hit"),
    [
        # What a missed `<scratch>` replacement leaves.
        ("/private/var/folders/ab/cd1234/T/tmpq/a", "/private"),
        ("/var/folders/ab/cd1234/T/tmpq/a", "/var/folders"),
        ("C:\\Users\\fixtureuser\\AppData\\Local\\Temp\\tmpq\\a", ":\\Users"),
        ("/Users/fixtureuser/notes/a", "/Users"),
        ("E:\\Windows\\x", "E:\\Windows"),
        ("could not reach Mac: refused", "Mac"),
        ('{"p":"/srv/\\u004dac"}', "Mac"),
        # The fixture world's scratch path and HOME.
        ("<scratch>/a", None),
        ("<scratch>/home/x/a", None),
        ("<scratch>/home/fixtureuser/a", "fixtureuser"),
        # The exempt texts, in the bytes as in the text.
        ('{"path":"/tmp/a b"}', None),
        ("cwd: Malformed Mach-o file", None),
        # A synthetic value written as hex is exempt as the whole run only.
        ("\\\\?\\C:\\x", None),
        ("\\\\?\\C:\\xy", "\\\\?\\"),
        ("\\\\?\\C:\\", "\\\\?\\"),
        ('{"abs":"/tmp/x y/\u00e9.md","rel":"a/b.txt"}', None),
        ('{"abs":"/tmp/x y/\u00e9.md","rel":"a/b.txt "}', "/tmp"),
        # Three bytes or more: a three-character name alone.
        (b"Mac.", "Mac"),
        (b"Mac", "Mac"),
    ],
)
def test_the_host_fact_guard_reads_bytes_written_as_hex(
    monkeypatch: pytest.MonkeyPatch, answer: str | bytes, hit: str | None
) -> None:
    """The fixtures write bytes as hex, some after replacing the run's
    scratch path by `<scratch>`: a host path or name that replacement
    misses is caught in the bytes the hex spells."""
    import getpass
    import socket

    from tests.contract.gen_store_files import host_fact

    monkeypatch.setattr(getpass, "getuser", lambda: "fixtureuser")
    monkeypatch.setattr(socket, "gethostname", lambda: "Mac.local")
    for data in (_hex_row(answer), _hex_row(answer, upper=True)):
        assert b"Mac" not in data and b"fixtureuser" not in data
        assert host_fact(data) == hit


@pytest.mark.parametrize(
    "spell",
    [
        # As text: UTF-8, then Latin-1.
        lambda path: path.encode(),
        lambda path: path.encode("latin-1"),
        # The same bytes written as hex.
        lambda path: _hex_row(path.encode()),
        lambda path: _hex_row(path.encode("latin-1")),
    ],
    ids=["utf-8", "latin-1", "utf-8-hex", "latin-1-hex"],
)
def test_the_host_fact_guard_reads_utf_8_and_latin_1(
    monkeypatch: pytest.MonkeyPatch, spell: Callable[[str], bytes]
) -> None:
    """A name holding a character outside ASCII is read in UTF-8 bytes and
    in Latin-1 bytes, as text and as hex."""
    import getpass
    import socket

    from tests.contract.gen_store_files import host_fact

    name = "fixtureus\u00e9r"
    monkeypatch.setattr(getpass, "getuser", lambda: name)
    monkeypatch.setattr(socket, "gethostname", lambda: "Mac.local")
    assert host_fact(spell(f"/srv/{name}/x")) == name


def test_the_errno_generator_names_the_errno_its_numbers_must_run_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the platform's `errno` module names the last number of
    ``ERRNO_NUMBERS``'s run from 0 (133, as CPython 3.14 on Linux names
    EHWPOISON), the generator stops and says what to change.

    The platform's table is replaced, not added to, by its numbers below
    that one and that one: on Windows `errno` names the Winsock errnos
    too, from 10004 up, so there the last errno is one of those."""
    import errno
    import re

    from tests.contract.gen_location_probe import ERRNO_NUMBERS, generate_errno

    end = next(n for n in range(2**16) if n + 1 not in ERRNO_NUMBERS)
    below = {n: name for n, name in errno.errorcode.items() if n < end}
    monkeypatch.setattr(errno, "errorcode", {**below, end: "ELAST"})
    want = (
        f"ERRNO_NUMBERS must run past this platform's last errno, {end}"
        f" (ELAST), and lacks {end + 1}: extend its range to range({end + 2})"
        " and regenerate location_probe_errno.jsonl"
    )
    with pytest.raises(RuntimeError, match=f"^{re.escape(want)}$"):
        generate_errno()


def test_golden_files_stay_small() -> None:
    total = sum(p.stat().st_size for p in GOLDEN.iterdir() if p.is_file())
    # The 382 fixtures totalled 20.9 MiB in November 2026. Leave room for
    # the remaining port units and catch accidental bulk output. Each new
    # fixture still has its own size budget when its unit is reviewed.
    assert total < 64 * 1024 * 1024


def test_no_fixture_line_is_shaped_like_a_key() -> None:
    """No line of a golden file, of a generator or of the native build's
    sources and tests is a text the product's own detectors take for a
    credential. The fixtures of the detectors hold key shapes by nature;
    they are written in pieces (`gen_credentials_prefixed._s` and its
    recipes, `concat!` in Rust), because a scanner that reads the
    repository takes a whole one for a leaked key: GitHub's push
    protection refused the push that first held them."""
    from bettermemory import credentials

    repo = Path(__file__).resolve().parents[1]
    native = repo / "native"
    paths = [
        *sorted(p for p in GOLDEN.iterdir() if p.is_file()),
        *sorted(GOLDEN.parent.glob("gen_*.py")),
        *sorted((native / "crates").rglob("*.rs")),
        native / "README.md",
    ]
    aws_example = "AKIA" + "IOSFODNN7EXAMPLE"
    shaped = []
    for path in paths:
        if path.suffix == ".sqlite":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            # AWS's documented example key, which scanners let pass, is
            # the one whole shape older fixtures hold.
            if credentials.find_credential_markers(line.replace(aws_example, "")):
                shaped.append(f"{path.relative_to(repo)}:{number}")
    assert not shaped, shaped[:20]


def _fixture_kinds() -> tuple[set[str], set[str]]:
    lines = (GOLDEN / "log_chain.jsonl").read_text(encoding="ascii").splitlines()
    header = json.loads(lines[0])
    rows = {json.loads(line)["row"]["kind"] for line in lines[1:] if '"row"' in line}
    return set(header["kinds"]) | {"store_created", "rekey"}, rows


def test_the_log_fixture_has_a_row_of_every_kind_the_code_writes() -> None:
    """A kind a call site or a table adds fails here until the log fixture
    is generated again with a row of it."""
    listed, rows = _fixture_kinds()
    missing = writable_kinds() - listed
    assert not missing, f"kinds the code writes that log_chain.jsonl lacks: {missing}"
    assert listed <= rows


def test_the_legacy_kinds_are_ones_the_code_no_longer_writes() -> None:
    assert not set(LEGACY_SHAPES) & writable_kinds()


def test_every_kind_site_is_read_or_forwarded_by_a_bounded_writer() -> None:
    """Each call that passes a kind it does not spell forwards one of the
    tables `kinds.table_kinds` bounds; a new forwarding site is listed
    here once it is known to forward only those."""
    _, forwarded = call_sites()
    sites = {
        entry.split(" ", 1)[0].rsplit(":", 1)[0] + " " + entry.split(" ", 1)[1]
        for entry in forwarded
    }
    assert sites <= _FORWARDING, sites - _FORWARDING


_FORWARDING = frozenset(
    {
        "events.py record_event(kind)",
        "handlers/_guard.py record(deps)",
        "handlers/standing_review.py record(deps)",
        "handlers/write.py record(deps)",
        "key_lifecycle.py _append(kind)",
        "log.py _append(kind)",
        "membership.py record_membership(event_kind)",
        "membership.py _event('checkout_moved' if moved else 'checkout_view_added')",
        "membership_migrate.py record_membership(kind)",
        "recall.py record([])",
        "recall.py record(ids)",
        "store.py _append(kind)",
        "store_events.py _append(kind)",
        "store_membership.py _mutate(f'{spec.kind}_put')",
        "store_membership.py _mutate(f'{spec.kind}_delete')",
        "store_membership.py _append(kind)",
    }
)


# ---------------------------------------------------------------------------
# The placeholders (`tests/contract/_sandbox.Scrubber`), on every platform's
# spelling of a scratch path, since each CI leg only meets its own.
# ---------------------------------------------------------------------------

_SCRUB_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("root", "text", "expected"),
    [
        (
            "/tmp/abc",
            "/tmp/abc/mem/store/memory.sqlite",
            "<scratch>/mem/store/memory.sqlite",
        ),
        (
            "/private/var/folders/x/T/abc",
            "cwd /private/var/folders/x/T/abc/alpha.",
            "cwd <scratch>/alpha.",
        ),
        (
            "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\abc",
            "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\abc\\mem\\store\\memory.sqlite",
            "<scratch>/mem/store/memory.sqlite",
        ),
        (
            "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\abc",
            '{"cwd": "C:\\\\Users\\\\RUNNER~1\\\\AppData\\\\Local\\\\Temp\\\\abc\\\\alpha\\\\x.md"}',
            '{"cwd": "<scratch>/alpha/x.md"}',
        ),
        (
            "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\abc",
            "C:/Users/RUNNER~1/AppData/Local/Temp/abc/alpha",
            "<scratch>/alpha",
        ),
    ],
)
def test_scrubber_writes_every_scratch_path_alike(
    root: str, text: str, expected: str
) -> None:
    assert Scrubber(root, now=_SCRUB_NOW).text(text) == expected


def test_scrubber_keeps_what_the_run_did_not_mint() -> None:
    scrub = Scrubber("/tmp/abc", now=_SCRUB_NOW)
    fixed = "01ARZ3NDEKTSV4RRFFQ69G5FAV 2016-07-30 2026-01-01T00:00:00Z"
    assert scrub.text(fixed) == fixed
    minted = "01KT1GW6G00000000000000000 2026-06-01 2026-06-02T03:04:05.6Z"
    assert scrub.text(minted) == "<ulid:1> <date> <timestamp>"
    assert scrub.text("8081851b1353b84897f648a4c366460539280d68") == (
        "8081851b1353b84897f648a4c366460539280d68"
    )
