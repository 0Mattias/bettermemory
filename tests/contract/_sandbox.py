"""A scratch world for the fixtures that drive the served product (tools,
hooks, the daemon's HTTP API).

Every location bettermemory resolves through the environment points into
one temporary directory, the process works from a git checkout there
(fixed identity, fixed dates, a remote under ``example.invalid``), and
everything is put back on exit. `Scrubber` then writes the values a run
cannot fix (ids and times minted from the wall clock and a random source,
tokens under a random key, the scratch directory's own path) as typed
placeholders.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory import _daemon_client
from bettermemory import config as bconfig

from .gen_store_path import _neutral_git, _repo

#: The variables a location resolves through, each pointed into the
#: scratch directory (every harness and bettermemory variable cleared first).
_LOCATION_ENVS = {
    "HOME": "home",
    "USERPROFILE": "home",
    "XDG_CONFIG_HOME": "home/.config",
    "XDG_STATE_HOME": "home/.local/state",
    "XDG_DATA_HOME": "home/.local/share",
    "XDG_CACHE_HOME": "home/.cache",
    "WIN_PD_OVERRIDE_APPDATA": "home/AppData/Roaming",
    "WIN_PD_OVERRIDE_LOCAL_APPDATA": "home/AppData/Local",
    bconfig.ENV_DIR_OVERRIDE: "mem",
    bconfig.KEYS_DIR_ENV: "keys",
    _daemon_client.STATE_DIR_ENV: "state",
}
#: Every variable a harness or an earlier bettermemory set is cleared first
#: (the harness's session id, its attended flag, a config override).
_CLEARED_PREFIXES = ("CLAUDE", "BETTERMEMORY_")


@dataclass(frozen=True)
class World:
    root: Path
    checkout: Path
    memory_dir: Path
    keys_dir: Path
    #: The run's clock reading as the world was made: the placeholders'
    #: window is read from it.
    now: datetime


@contextmanager
def world() -> Iterator[World]:
    saved_env = dict(os.environ)
    saved_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as raw, _neutral_git(Path(raw)):
        root = Path(raw)
        try:
            for name in list(os.environ):
                if name.startswith(_CLEARED_PREFIXES):
                    del os.environ[name]
            for name, rel in _LOCATION_ENVS.items():
                (root / rel).mkdir(parents=True, exist_ok=True)
                os.environ[name] = str(root / rel)
            _repo(root / "alpha", "https://example.invalid/fixture/alpha.git", 1)
            os.chdir(root / "alpha")
            yield World(
                root=root,
                checkout=root / "alpha",
                memory_dir=root / "mem",
                keys_dir=root / "keys",
                now=datetime.now(timezone.utc),
            )
        finally:
            os.chdir(saved_cwd)
            os.environ.clear()
            os.environ.update(saved_env)


#: Placeholders. A numbered one keeps the same number for the same value
#: throughout a file, so a reference (an id a later request sends back)
#: still reads as the same value. Only values the run mints from the wall
#: clock or a random source are replaced: a fixed id, a fixed date, a git
#: SHA of the fixture repository stay literal.
PLACEHOLDERS: dict[str, str] = {
    "<scratch>": (
        "the run's temporary directory (an absolute path); the path after it "
        "is written with / whatever the platform's separator"
    ),
    "<ulid:N>": (
        "a ULID minted during the run (its time part within 31 days of the "
        "run's clock); N numbers distinct values in order of first "
        "appearance in the file"
    ),
    "<session:N>": "a session id the server mints, sess_ and 16 lowercase hex",
    "<use_token>": "a use token, use_ and 16 lowercase hex, random per hit",
    "capture:<batch:N>": (
        "a capture batch's source tag, capture: and 16 lowercase hex, random; "
        "N numbers distinct batches"
    ),
    "<timestamp>": (
        "an ISO-8601 time on a date within 31 days of the run's clock "
        "(YYYY-MM-DDTHH:MM:SS, optional fraction, Z or offset)"
    ),
    "<date>": "a YYYY-MM-DD date within 31 days of the run's clock",
}

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID = re.compile(r"(?<![0-9A-Za-z])[0-9A-HJKMNP-TV-Z]{26}(?![0-9A-Za-z])")
_SESSION = re.compile(r"sess_[0-9a-f]{16}")
_USE = re.compile(r"use_[0-9a-f]{16}")
_BATCH = re.compile(r"capture:([0-9a-f]{16})")
_STAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)
_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
#: The path after `<scratch>`: components joined by /, \ or (JSON-escaped)
#: \\.
_TAIL = re.compile(r"<scratch>((?:(?:\\\\|\\|/)[A-Za-z0-9._-]+)+)")
_SEPARATOR = re.compile(r"\\\\|\\")
_WINDOW = timedelta(days=31)


def ulid_time(value: str) -> datetime:
    ms = 0
    for ch in value[:10]:
        ms = ms * 32 + _CROCKFORD.index(ch)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def root_spellings(root: str) -> list[str]:
    """`root` as it may appear in output: as given, resolved, with forward
    slashes, and each of those JSON-escaped; longest first."""
    plain = {root, root.replace("\\", "/")}
    if os.path.exists(root):
        plain.add(os.path.realpath(root))
    every = plain | {json.dumps(p)[1:-1] for p in plain}
    return sorted(every, key=len, reverse=True)


class Scrubber:
    def __init__(
        self,
        root: str | Path,
        *,
        now: datetime,
        keep: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self._keep = frozenset(keep)
        self._roots = root_spellings(str(root))
        self._now = now
        self._dates = {
            (now.date() + timedelta(days=d)).isoformat() for d in range(-31, 32)
        }
        self._ulids: dict[str, int] = {}
        self._sessions: dict[str, int] = {}
        self._batches: dict[str, int] = {}

    def _number(self, table: dict[str, int], value: str) -> int:
        return table.setdefault(value, len(table) + 1)

    def _ulid(self, match: re.Match[str]) -> str:
        value = match.group()
        if value in self._keep or abs(ulid_time(value) - self._now) > _WINDOW:
            return value
        return f"<ulid:{self._number(self._ulids, value)}>"

    def paths(self, value: str) -> str:
        for spelling in self._roots:
            value = value.replace(spelling, "<scratch>")
        return _TAIL.sub(lambda m: "<scratch>" + _SEPARATOR.sub("/", m.group(1)), value)

    def text(self, value: str) -> str:
        value = self.paths(value)
        value = _SESSION.sub(
            lambda m: f"<session:{self._number(self._sessions, m.group())}>", value
        )
        value = _USE.sub("<use_token>", value)
        value = _BATCH.sub(
            lambda m: f"capture:<batch:{self._number(self._batches, m.group(1))}>",
            value,
        )
        value = _ULID.sub(self._ulid, value)
        value = _STAMP.sub(
            lambda m: "<timestamp>" if m.group(1) in self._dates else m.group(), value
        )
        return _DATE.sub(
            lambda m: "<date>" if m.group() in self._dates else m.group(), value
        )

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        return value
