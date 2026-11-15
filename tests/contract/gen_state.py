"""Fixture 11: the files on disk two builds share (``state_files.json``,
ungated).

Produced by running the product in the scratch world and reading back what
it wrote:

- the store directory and file `Store.create` makes, and the key ring
  beside it in the keys directory: the key, the head checkpoint, the
  binding (``.path``) file, the origin anchor and their lock files. The
  store id and the key are the fixed ones the log fixture uses (the
  product's random source, `secrets.token_hex` / `token_bytes`, answers
  them), so file names and the key fingerprint are literal; each file's
  exact text is given, since a native build must write what this one
  reads;
- the daemon's state file as `daemon._write_daemon_state` writes it, and
  the names of its lock, spawn lock and log, with the name derivation
  (`_daemon_client.state_file_for`) over fixed canonical paths;
- a backup bundle (`backup.create_backup`, with its key, at a fixed time):
  the files it holds and its manifest;
- the names of the files the hook client and the daemon keep in the state
  directory besides (`gitsystem`, `gitdir_pin`, `machine`), not exercised
  here.

Modes are the ones the product asks for; on POSIX the generator checks
each observed mode equals it, and on Windows, which has no such modes,
it checks nothing and writes the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bettermemory
from bettermemory import _daemon_client, daemon, gitdir_pin, gitsystem, machine
from bettermemory.backup import MANIFEST_NAME, create_backup
from bettermemory.store import Store

from ._common import json_doc
from ._sandbox import PLACEHOLDERS, Scrubber, world
from .gen_daemon import PORT, TOKEN, VERSION
from .gen_log import KEY_A, STORE_ID

#: The moment the backup is taken at (`create_backup(now=...)`): far from
#: any run's clock, so no placeholder can take it.
BACKUP_AT = datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)

#: Canonical store paths the daemon's file names are derived from, to show
#: the derivation (`state_file_for`) without a scratch path in it.
_CANONICAL = [
    "/home/fixture/.local/share/bettermemory/store/memory.sqlite",
    "/srv/repo/.claude-memory/store/memory.sqlite",
    "C:\\Users\\fixture\\AppData\\Local\\bettermemory\\store\\memory.sqlite",
]

#: The shape every <timestamp> in this file stands for: UTC, `Z`, never
#: `+00:00`, six fraction digits, the fraction left out when it is zero
#: (`datetime.isoformat`). Checked against the real values.
_ISO_Z = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?Z"

_HEX64 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")

PLACEHOLDERS_HERE = {
    "<scratch>": PLACEHOLDERS["<scratch>"],
    "<timestamp>": PLACEHOLDERS["<timestamp>"],
    "<hex64:N>": (
        "a 64-hex-digit value the run cannot fix: an HMAC-SHA256 over the "
        "store_created row, whose time is the run's clock, or a digest of a "
        "file holding it; N numbers distinct values in order of first "
        "appearance in the file. The key fingerprint, from the fixed key, "
        "stays literal"
    ),
    "<pid>": "the writing process's id (an integer in the file)",
    "<digest16>": (
        "the first 16 hex digits of sha256 over the scratch store's canonical "
        "path (name_derivation shows the rule over fixed paths)"
    ),
    "<snapshot_size>": "the byte size of the store snapshot (an integer)",
}


@contextmanager
def _fixed_random() -> Iterator[None]:
    """The store id, the membership id and the key `Store.create` mints;
    every later draw stays random."""
    hexes = [STORE_ID, "fedcba9876543210fedcba9876543210"]
    keys = [KEY_A]

    def token_hex(nbytes: int | None = None) -> str:
        # The first two are the ids; the rest (a digest token in the
        # store) reach no byte here.
        return hexes.pop(0) if hexes else os.urandom(nbytes or 32).hex()

    def token_bytes(nbytes: int | None = None) -> bytes:
        if nbytes == len(KEY_A) and keys:
            return keys.pop()
        return os.urandom(nbytes or 32)

    with (
        patch.object(secrets, "token_hex", token_hex),
        patch.object(secrets, "token_bytes", token_bytes),
    ):
        yield


class _Hex:
    def __init__(self, keep: set[str]) -> None:
        self.keep = keep
        self.seen: dict[str, int] = {}

    def __call__(self, text: str) -> str:
        def sub(m: re.Match[str]) -> str:
            if m.group() in self.keep:
                return m.group()
            return f"<hex64:{self.seen.setdefault(m.group(), len(self.seen) + 1)}>"

        return _HEX64.sub(sub, text)


def _mode(path: Path, wanted: int) -> str:
    if os.name == "posix":
        seen = stat.S_IMODE(os.lstat(path).st_mode)
        if seen != wanted:
            raise AssertionError(f"{path}: mode {seen:o}, expected {wanted:o}")
    return f"{wanted:04o}"


def _entry(
    path: Path, root: Path, mode: int, scrub: Any, **extra: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": path.relative_to(root).as_posix(),
        "mode": _mode(path, mode),
        **extra,
    }
    if path.is_file() and path.suffix in (".head", ".path", ".origin", ".json"):
        text = path.read_text(encoding="utf-8")
        out["text"] = scrub(text)
    return out


def generate() -> dict[str, bytes]:
    with world() as w, _fixed_random():
        clean = Scrubber(w.root, now=w.now)
        fingerprint = hashlib.sha256(KEY_A).hexdigest()
        hexes = _Hex({fingerprint})

        def scrub(text: str) -> str:
            return str(clean(hexes(text)))

        store = Store.create(w.memory_dir / "store", keys_dir=w.keys_dir)
        store_file = Path(store.path)
        try:
            state_dir = w.root / "state" / "daemon"
            with patch.object(daemon, "_version", lambda: VERSION):
                daemon._write_daemon_state(state_dir, store, TOKEN, PORT, "127.0.0.1")
        finally:
            store.close()

        store_files = [
            _entry(w.memory_dir / "store", w.memory_dir, 0o700, scrub, kind="dir"),
            _entry(store_file, w.memory_dir, 0o600, scrub, kind="sqlite store"),
        ]
        ring = {
            ".key": "the key: 32 raw bytes (here the fixed test key 000102...1f); its "
            "fingerprint is sha256 of the bytes",
            ".head": "the head checkpoint: JSON {seq, mac, ts}, the last row's seq and "
            "MAC and the time it was written; rewritten on every append",
            ".head.lock": "an empty lock file, locked while .head is rewritten",
            ".path": "the binding: JSON {path}, the canonical store file this key "
            "belongs to",
            ".path.lock": "an empty lock file, locked while .path is rewritten",
            ".origin": "the origin anchor: JSON {seq, mac, key_fingerprint}, the "
            "first row this machine signed",
            ".origin.lock": "an empty lock file, locked while .origin is written",
        }
        names = sorted(p.name for p in w.keys_dir.iterdir())
        expected = sorted(STORE_ID + suffix for suffix in ring)
        if names != expected:
            raise AssertionError(f"key ring files: {names}")
        keys_files = [
            _entry(
                w.keys_dir / (STORE_ID + suffix), w.keys_dir, 0o600, scrub, holds=what
            )
            for suffix, what in ring.items()
        ]
        key_bytes = (w.keys_dir / f"{STORE_ID}.key").read_bytes()
        if key_bytes != KEY_A:
            raise AssertionError("the key file does not hold the fixed key")

        # The daemon's state file.
        (state_file,) = list(state_dir.iterdir())
        canonical = _daemon_client.canonical_store_path(str(store_file))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        if state_file.name != f"daemon-{digest}.json":
            raise AssertionError(state_file.name)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        head = json.loads((w.keys_dir / f"{STORE_ID}.head").read_text("utf-8"))
        for where, value in (
            ("daemon_state.started", state["started"]),
            ("key_ring.head.ts", head["ts"]),
        ):
            if not re.fullmatch(_ISO_Z, value):
                raise AssertionError(f"{where}: {value!r} is not {_ISO_Z}")
        text = state_file.read_text(encoding="utf-8").replace(
            f'"pid": {state["pid"]}', '"pid": <pid>'
        )
        daemon_state = {
            "path": "daemon-<digest16>.json",
            "mode": _mode(state_file, 0o600),
            "directory_mode": _mode(state_dir, 0o700),
            "text": scrub(text),
            "fields": {
                k: type(v).__name__.replace("str", "string").replace("int", "integer")
                for k, v in sorted(state.items())
            },
            "siblings": {
                "lock": _daemon_client.lock_file_for(
                    Path("s"), store_file
                ).name.replace(digest, "<digest16>"),
                "spawn_lock": _daemon_client.spawn_lock_file_for(
                    Path("s"), store_file
                ).name.replace(digest, "<digest16>"),
                "log": _daemon_client.log_file_for(Path("s"), store_file).name.replace(
                    digest, "<digest16>"
                ),
            },
        }
        with patch.object(_daemon_client, "canonical_store_path", lambda p: p):
            derivation = [
                {
                    "canonical": c,
                    "state_file": _daemon_client.state_file_for(Path("s"), c).name,
                    "observed": False,
                }
                for c in _CANONICAL
            ]

        # A backup bundle.
        with patch.object(bettermemory, "__version__", VERSION):
            result = create_backup(
                store_file, keys_dir=w.keys_dir, include_key=True, now=BACKUP_AT
            )
        bundle = Path(result["bundle"])
        manifest_text = (bundle / MANIFEST_NAME).read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        size = manifest["files"]["memory.sqlite"]["size"]
        manifest_text = manifest_text.replace(
            f'"size": {size}', '"size": <snapshot_size>', 1
        )
        bundle_files = [
            _entry(bundle, w.memory_dir, 0o700, lambda t: t, kind="dir"),
            *(
                _entry(p, w.memory_dir, 0o600, scrub)
                for p in sorted(bundle.iterdir())
                if p.name != MANIFEST_NAME
            ),
        ]
        backup = {
            "bundle": bundle.relative_to(w.memory_dir).as_posix(),
            "files": bundle_files,
            "manifest_mode": _mode(bundle / MANIFEST_NAME, 0o600),
            "manifest_text": scrub(manifest_text),
        }

    doc = {
        "fixture": "state_files",
        "read": (
            "Each file: path relative to its root (store: <scratch>/mem, the "
            "BETTERMEMORY_DIR; keys: <scratch>/keys, the BETTERMEMORY_KEYS_DIR; "
            "the daemon's files: the state directory), mode as an octal string "
            "(the mode asked for; POSIX only), and for a text file its exact "
            "text with the run's values as placeholders. writers_readers names "
            "the components that write and read each. Every JSON text here is "
            "written by Python's json.dumps with its default separators "
            '(", ", ": "); the state file and the manifest sort their keys, '
            "the key ring's files keep the order shown."
        ),
        "setup": (
            "_sandbox.world; Store.create(<scratch>/mem/store, keys_dir="
            f"<scratch>/keys) with secrets.token_hex answering {STORE_ID} "
            "(the store id) then fedcba9876543210fedcba9876543210 (the "
            "membership id) and secrets.token_bytes(32) answering the log "
            "fixture's KEY_A (bytes 0..31); then daemon._write_daemon_state("
            f"<scratch>/state/daemon, store, token {TOKEN!r}, port {PORT}, "
            f"'127.0.0.1') with the version {VERSION!r}; then create_backup("
            "store file, include_key=True, now=2001-02-03T04:05:06Z) with "
            f"bettermemory.__version__ = {VERSION!r}."
        ),
        "placeholders": PLACEHOLDERS_HERE,
        "timestamp_format": {
            "applies_to": ["daemon_state.started", "key_ring .head ts"],
            "regex": _ISO_Z,
            "rule": (
                "UTC written with Z (never +00:00), seconds, then a fraction of "
                "exactly six digits unless the microsecond is 0, when the "
                "fraction is left out (Python's datetime.isoformat). The "
                "backup's created_at and its head ts are fixed values of the "
                "same writer, here without a fraction."
            ),
        },
        "store_id": STORE_ID,
        "key_fingerprint": fingerprint,
        "store": store_files,
        "key_ring": keys_files,
        "daemon_state": daemon_state,
        "name_derivation": {
            "rule": (
                "daemon-<first 16 hex of sha256(UTF-8 of the canonical store "
                "file path)>.json; the lock, spawn lock and log replace .json "
                "with .lock, .spawn.lock, .log. Canonical: realpath, then on "
                "POSIX each existing component spelled as its directory lists "
                "it where the directory folds case or normalization "
                "(_daemon_client.canonical_store_path); on Windows realpath."
            ),
            "examples": derivation,
            "examples_note": (
                "observed false: each example is the rule computed over the "
                "given string as if it were already canonical (the "
                "canonicalising function replaced by the identity), not a "
                "path observed on that platform"
            ),
        },
        "backup": backup,
        "state_directory_names": {
            "git_config_record": gitsystem.RECORD,
            "git_ask_folder": gitsystem.ASK_FOLDER,
            "git_probe_folder": gitsystem.PROBE_FOLDER,
            "gitdir_pins_folder": gitdir_pin.PINS_DIR,
            "machine_id": machine.MACHINE_ID_FILE,
            "machine_facts": machine.MACHINE_FACTS_FILE,
            "note": (
                "names only: these files hold host facts (git's config "
                "sources, gitdir pins, the machine's id and facts) and are "
                "not written by this fixture"
            ),
        },
        "writers_readers": {
            "store": "written by every opener of the store (daemon, CLI, stdio "
            "server); read by the same",
            "key_ring": "written by Store.create, every append (.head), a rekey "
            "(.key, .origin) and a store move (.path); read by every opener, "
            "security audit and backup",
            "daemon_state": "written by the daemon at start, removed at a clean "
            "stop; read by the hook client, the shim and up/down/status, which "
            "trust it only after /health?nonce= proves the token",
            "backup": "written by backup create; read by backup verify and restore",
        },
    }
    return {"state_files.json": json_doc(doc)}
