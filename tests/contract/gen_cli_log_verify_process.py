"""``bettermemory log verify`` as a process over real stores (POSIX).

Every case rebuilds a store of `store_bases.json` under a fresh root, edits
it, and runs ``python -m bettermemory`` as a child with the root as its
home, config, store, keys and state directories. The row holds the exit
status, both streams with the root spelled ``<root>``, and the names left
under the root, so a build is held to what the command printed, to the
warnings its logging wrote before that, and to what it left on disk.

A child, not a call: the reference's warnings reach stderr through
`logging`'s handler of last resort and are told once per process, and both
are the process's.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import gen_store_files
from ._common import jsonl
from .gen_log import STORE_ID

ROOT = "<root>"
_OTHER_ID = "f" * 32
_ZERO = "0" * 64

#: Store cases whose JSON report is compared too.
_JSON = (
    "intact",
    "rekeyed",
    "restored",
    "accepted",
    "no_head",
    "origin_broken",
    "tampered_payload",
    "deleted_row",
    "retired_key_missing",
    "writer_above",
)

#: A case of `store_files.jsonl` the reference does not refuse: it raises
#: TypeError out of `store_admission.schema_refusal` and prints a
#: traceback, whose text is the interpreter's and the checkout's.
_TRACEBACK = ("blob_sql",)


def _altered(taken: str, **fields: Any) -> str:
    """The taken anchor with `fields` replaced, as one line."""
    return json.dumps({**json.loads(taken), **fields})


def _taken(bases: dict[str, Any]) -> str:
    """The anchor `log anchor` takes of the intact store, at a fixed time."""
    from bettermemory import anchor
    from bettermemory.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        case = Path(tmp).resolve() / "store"
        gen_store_files.rebuild(bases["intact"], case)
        store = Store.open(
            case / "memory.sqlite", keys_dir=case / "keys", read_only=True
        )
        try:
            taken = anchor.take(store, now=datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC))
        finally:
            store.close()
        return taken.to_line()


def cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """Each case: the store it starts from, the edits, the files, the call."""
    verify = ["log", "verify"]
    out: list[dict[str, Any]] = []

    def add(name: str, store: str | None, argv: list[str], **more: Any) -> None:
        out.append({"case": name, "store": store, "argv": argv, **more})

    for name in gen_store_files.cases(bases):
        if name in _TRACEBACK:
            continue
        add(name, name, verify)
        if name in _JSON:
            add(f"{name}_json", name, [*verify, "--json"])

    key = bytes(range(32, 64)).hex()
    for name, ops in (
        ("key_missing", [{"unlink": ".key"}]),
        ("key_foreign", [{"write": ".key", "hex": key}]),
        ("key_empty", [{"write": ".key", "hex": ""}]),
        ("key_unreadable", [{"chmod": ".key", "mode": 0}]),
        ("head_not_json", [{"write": ".head", "hex": b"{not json".hex()}]),
        ("origin_not_json", [{"write": ".origin", "hex": b"[1,2".hex()}]),
        ("binding_not_json", [{"key_text": ".path", "text": "{{{"}]),
        (
            "binding_relative",
            [{"key_text": ".path", "text": '{"path": "x/memory.sqlite"}'}],
        ),
        (
            "bound_here",
            [{"key_text": ".path", "text": '{"path": "<root>/store/memory.sqlite"}'}],
        ),
        (
            "bound_elsewhere",
            [
                {"copy_store": "other"},
                {
                    "key_text": ".path",
                    "text": '{"path": "<root>/other/memory.sqlite"}',
                },
            ],
        ),
        (
            "bound_gone",
            [{"key_text": ".path", "text": '{"path": "<root>/gone/memory.sqlite"}'}],
        ),
    ):
        add(name, "intact", verify, ops=ops)
        add(f"{name}_json", "intact", [*verify, "--json"], ops=ops)

    taken = _taken(bases)
    other = _altered(taken, store_id=_OTHER_ID)
    wrong = _altered(taken, mac=_ZERO)
    redigested = _altered(taken, chain_sha256=_ZERO)
    past = _altered(taken, seq=10**6)
    unformatted = _altered(taken, format="bettermemory.anchor/0")
    anchored = [*verify, "--anchor", "a.json"]
    for name, text, argv in (
        ("anchor_taken", taken, anchored),
        ("anchor_taken_json", taken, [*anchored, "--json"]),
        (
            "anchor_taken_twice",
            taken + taken,
            [*verify, "--json", "--anchor=a.json", "--a", "a.json"],
        ),
        ("anchor_other_store", other, anchored),
        ("anchor_other_store_json", other, [*anchored, "--json"]),
        ("anchor_wrong", wrong, anchored),
        ("anchor_wrong_json", wrong, [*anchored, "--json"]),
        ("anchor_other_digest", redigested, anchored),
        ("anchor_past_the_log", past, anchored),
        ("anchor_past_the_log_json", past, [*anchored, "--json"]),
        ("anchor_of_another_format", unformatted, anchored),
        ("anchor_taken_and_other", taken + other + "\n", anchored),
        ("anchor_not_json", "not json", anchored),
        ("anchor_empty_file", "", anchored),
        ("anchor_empty_object", "{}", anchored),
        ("anchor_a_list", "[1]", anchored),
    ):
        add(name, "intact", argv, files={"me/a.json": text})
    add("anchor_missing_file", "intact", [*verify, "--anchor", "missing.json"])
    add(
        "anchor_taken_then_tampered",
        "tampered_payload",
        anchored,
        files={"me/a.json": taken},
    )
    # A file that is not an anchor is a usage error before the store is
    # looked for.
    add(
        "anchor_not_json_and_no_store",
        None,
        anchored,
        files={"me/a.json": "not json"},
    )

    add("no_store", None, verify)
    add("no_store_json", None, [*verify, "--json"])
    add("no_store_narrow", None, verify, env={"COLUMNS": "20"})
    add("store_dir_is_a_file", None, verify, files={"store": "x"})
    add(
        "store_file_is_garbage", None, verify, files={"store/memory.sqlite": "g" * 2100}
    )
    add("store_file_is_empty", None, verify, files={"store/memory.sqlite": ""})
    add("store_file_is_a_directory", None, verify, files={"store/memory.sqlite/x": ""})
    add("store_file_unreadable", "intact", verify, ops=[{"chmod_store": 0}])
    add(
        "config_does_not_parse",
        "intact",
        verify,
        files={"xdg/bettermemory/config.toml": "not = [toml"},
    )
    add(
        "config_unknown_keys",
        "intact",
        verify,
        files={
            "xdg/bettermemory/config.toml": '[storage]\ndirektory = "x"\n[nope]\na = 1\n'
        },
    )
    add(
        "config_names_the_store",
        "intact",
        verify,
        ops=[{"move_store": "me/alt"}],
        files={
            "xdg/bettermemory/config.toml": '[storage]\ndirectory = "<root>/me/alt"\n'
        },
        env={"BETTERMEMORY_DIR": None, "BETTERMEMORY_KEYS_DIR": "<root>/me/alt/keys"},
    )
    add(
        "store_under_the_working_directory",
        "intact",
        verify,
        ops=[{"move_store": "me/.claude-memory"}],
        env={
            "BETTERMEMORY_DIR": None,
            "BETTERMEMORY_KEYS_DIR": "<root>/me/.claude-memory/keys",
        },
    )
    add(
        "store_dir_relative",
        "intact",
        verify,
        ops=[{"move_store": "me/rel"}],
        env={"BETTERMEMORY_DIR": "rel", "BETTERMEMORY_KEYS_DIR": "rel/keys"},
    )
    add(
        "store_dir_names_the_file",
        "intact",
        verify,
        env={"BETTERMEMORY_DIR": "<root>/store/memory.sqlite"},
    )
    add(
        "keys_dir_absent",
        "intact",
        verify,
        env={"BETTERMEMORY_KEYS_DIR": "<root>/nokeys"},
    )
    add(
        "keys_dir_absent_json",
        "intact",
        [*verify, "--json"],
        env={"BETTERMEMORY_KEYS_DIR": "<root>/nokeys"},
    )
    # What the open logs before the command's last line, and the order.
    removed = (
        "[behavior]\ncorroboration_boost = true\nrequire_write_confirmation = false\n"
        "[telemetry]\nmax_bytes = 5\n"
    )
    config = "xdg/bettermemory/config.toml"
    add("config_removed_keys", "intact", verify, files={config: removed})
    add("config_removed_keys_and_no_store", None, verify, files={config: removed})
    add(
        "config_removed_keys_and_no_load",
        "intact",
        verify,
        files={config: removed + "[storage]\ndirectory = 5\n"},
        env={"BETTERMEMORY_DIR": None},
    )
    add(
        "store_dir_under_a_system_directory",
        None,
        verify,
        env={"BETTERMEMORY_DIR": "/usr/share/bettermemory-absent-store"},
    )
    add("restore_running", "intact", verify, ops=[{"hold_restore": True}])
    add("temp_dir_absent", "intact", verify, env={"TMPDIR": "<root>/no-such-tmp"})
    add(
        "wal_unreadable",
        "intact",
        verify,
        files={"store/memory.sqlite-wal": ""},
        ops=[{"chmod_path": "store/memory.sqlite-wal", "mode": 0}],
    )
    blob = "UPDATE log SET payload = CAST(payload AS BLOB) WHERE seq = 2;"
    add("payload_a_blob", "intact", verify, ops=[{"sql": blob}])
    add("payload_a_blob_json", "intact", [*verify, "--json"], ops=[{"sql": blob}])
    add(
        "payload_a_number",
        "intact",
        verify,
        ops=[{"sql": "UPDATE log SET payload = 5 WHERE seq = 2;"}],
    )
    add(
        "meta_not_utf8",
        "intact",
        verify,
        ops=[{"sql": "INSERT INTO meta VALUES ('zz', CAST(x'ff' AS TEXT));"}],
    )
    add(
        "meta_without_a_fingerprint",
        "intact",
        verify,
        ops=[{"sql": "DELETE FROM meta WHERE key = 'key_fingerprint';"}],
    )
    # `Store.open` compares the tokenizer the log names with this build's.
    spelled = (
        "UPDATE log SET payload = json_set(payload, '$.tokenizer_fingerprint', "
        "'abababababababababab') WHERE kind = 'store_created';"
    )
    add("tokenizer_another", "intact", verify, ops=[{"sql": spelled}])
    add("tokenizer_another_json", "intact", [*verify, "--json"], ops=[{"sql": spelled}])
    add("log_table_gone", "intact", verify, ops=[{"sql": "DROP TABLE log;"}])
    add(
        "created_row_not_utf8",
        "intact",
        verify,
        ops=[
            {
                "sql": "UPDATE log SET payload = CAST(x'7bff7d' AS TEXT) "
                "WHERE kind = 'store_created';"
            }
        ],
    )
    add(
        "created_row_a_blob",
        "intact",
        verify,
        ops=[
            {
                "sql": "UPDATE log SET payload = CAST(payload AS BLOB) "
                "WHERE kind = 'store_created';"
            }
        ],
    )
    add("bare_log_beside_a_store", "intact", ["log"])
    add("verify_unknown_option", "intact", [*verify, "--nope"])
    add("verify_extra_argument", "intact", [*verify, "extra"])
    add("verify_json_abbreviated", "intact", [*verify, "--j"])
    return out


def _environment(root: Path, extra: dict[str, str | None]) -> dict[str, str]:
    """The child's whole environment: nothing of this process's but where
    its modules are, so a variable of the host (an output encoding, a
    colour switch, a config override) cannot reach a row."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root / "me"),
        "XDG_CONFIG_HOME": str(root / "xdg"),
        "BETTERMEMORY_DIR": str(root / "store"),
        "BETTERMEMORY_KEYS_DIR": str(root / "store" / "keys"),
        "BETTERMEMORY_STATE_DIR": str(root / "state"),
        "NO_COLOR": "1",
        "PYTHON_COLORS": "0",
        "COLUMNS": "80",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(entry for entry in sys.path if entry),
    }
    for name, value in extra.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value.replace(ROOT, str(root))
    return env


def _apply(ops: list[dict[str, Any]], root: Path) -> None:
    store = root / "store"
    for op in ops:
        if "chmod" in op:
            (store / "keys" / f"{STORE_ID}{op['chmod']}").chmod(int(op["mode"]))
        elif "chmod_store" in op:
            (store / "memory.sqlite").chmod(int(op["chmod_store"]))
        elif "chmod_path" in op:
            (root / str(op["chmod_path"])).chmod(int(op["mode"]))
        elif "hold_restore" in op:
            # Taken by `_run`, around the child.
            continue
        elif "key_text" in op:
            text = str(op["text"]).replace(ROOT, str(root))
            (store / "keys" / f"{STORE_ID}{op['key_text']}").write_text(
                text, encoding="utf-8"
            )
        elif "copy_store" in op:
            target = root / str(op["copy_store"])
            target.mkdir(parents=True)
            shutil.copyfile(store / "memory.sqlite", target / "memory.sqlite")
        elif "move_store" in op:
            target = root / str(op["move_store"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(store), str(target))
            store = target
        else:
            gen_store_files.apply([op], store)


def _left(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if not path.is_dir() or not any(path.iterdir())
    )


def _run(bases: dict[str, Any], case: dict[str, Any], scratch: Path) -> dict[str, Any]:
    root = scratch / str(case["case"])
    for name in ("me", "xdg", "state"):
        (root / name).mkdir(parents=True)
    if case["store"] is not None:
        _, base, recipe = gen_store_files.cases(bases)[case["store"]]
        gen_store_files._materialize(bases, base, recipe, root / "store")
    for name, text in case.get("files", {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text).replace(ROOT, str(root)), encoding="utf-8")
    _apply(case.get("ops", []), root)
    held: int | None = None
    hold = any("hold_restore" in op for op in case.get("ops", []))
    if hold and sys.platform != "win32":
        # `restore_lock`: a restore holds the store's directory locked
        # exclusively while it runs. POSIX's module, imported here: this
        # file is imported on Windows too, where its generator is not run.
        import fcntl

        held = os.open(root / "store", os.O_RDONLY)
        fcntl.flock(held, fcntl.LOCK_EX)
    # No `site`: a hook of the environment (a sitecustomize that points an
    # unset BETTERMEMORY_DIR somewhere) must not choose the store. The
    # child imports what this process imports.
    done = subprocess.run(
        [sys.executable, "-S", "-m", "bettermemory", *case["argv"]],
        cwd=root / "me",
        env=_environment(root, case.get("env", {})),
        capture_output=True,
        timeout=300,
        check=False,
    )
    if held is not None:
        os.close(held)
    for path in root.rglob("*"):
        if not path.is_symlink() and not os.access(path, os.R_OK):
            path.chmod(0o600)

    def scrubbed(raw: bytes) -> str:
        return raw.decode("utf-8").replace(str(root), ROOT)

    return {
        **case,
        "code": done.returncode,
        "out": scrubbed(done.stdout),
        "err": scrubbed(done.stderr),
        "left": _left(root),
    }


def generate() -> dict[str, bytes]:
    bases = gen_store_files.load_bases()
    todo = cases(bases)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp).resolve()
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda case: _run(bases, case, scratch), todo))
    for row in rows:
        if "Traceback (most recent call last)" in row["err"]:
            raise AssertionError(f"{row['case']}: the reference raised: {row['err']}")
    name = "cli_log_verify_process_posix"
    return {f"{name}.jsonl": jsonl([{"fixture": name, "cases": len(rows)}, *rows])}
