"""CAPS-P6: the capture unit left the MCP surface alone.

The `tools/list` array and the `initialize` reply's server instructions, as
`bench/toolcost/run.py`'s `probe_tools` reads them off the wire (a sandbox
HOME, hand-rolled JSON-RPC), for this tree and for a checkout of the
baseline revision, each through the shim (`python -m bettermemory`) and
the in-process server (`python -m bettermemory serve`). The tree is chosen
by PYTHONPATH, checked by importing the package from it. Bound: the sha256
and length of both are identical at HEAD and at the baseline. Revision 2
recorded 8,144 bytes for tools/list; the measured length is set beside it.

    git worktree add --detach /tmp/x eceb9a5
    python bench/capture/surface.py --baseline-tree /tmp/x --baseline-rev eceb9a5
    git worktree remove /tmp/x
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

sys.path.insert(0, str(_common.ROOT / "bench" / "toolcost"))
from run import measure, probe_tools  # type: ignore[import-not-found]  # noqa: E402

REVISION_2_TOOLS_LIST_BYTES = 8144
COMMANDS = {
    "shim": ["-m", "bettermemory"],
    "serve": ["-m", "bettermemory", "serve"],
}


def _blob(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def probe_tree(tree: Path) -> dict[str, Any]:
    env = {"PYTHONPATH": str(tree / "src")}
    where = subprocess.run(
        [_common.PYTHON, "-c", "import bettermemory; print(bettermemory.__file__)"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not Path(where).resolve().is_relative_to((tree / "src").resolve()):
        raise SystemExit(f"{tree}: bettermemory imported from {where}")
    out: dict[str, Any] = {}
    for label, args in COMMANDS.items():
        probe = probe_tools([_common.PYTHON, *args], env=env, cwd=str(tree))
        tools = _blob(probe.tools)
        instructions = probe.instructions.encode("utf-8")
        cost = measure(probe.tools, probe.instructions)
        out[label] = {
            "tool_count": cost["tool_count"],
            "tools_list_bytes": len(tools),
            "tools_list_sha256": _sha(tools),
            "instructions_bytes": len(instructions),
            "instructions_sha256": _sha(instructions),
            "session_bytes": cost["session_bytes"],
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-tree", type=Path, required=True)
    parser.add_argument("--baseline-rev", required=True)
    args = parser.parse_args()
    baseline_head = subprocess.run(
        ["git", "-C", str(args.baseline_tree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    head = probe_tree(_common.ROOT)
    base = probe_tree(args.baseline_tree)
    same = {
        label: {
            "tools_list": head[label]["tools_list_sha256"]
            == base[label]["tools_list_sha256"]
            and head[label]["tools_list_bytes"] == base[label]["tools_list_bytes"],
            "instructions": head[label]["instructions_sha256"]
            == base[label]["instructions_sha256"]
            and head[label]["instructions_bytes"] == base[label]["instructions_bytes"],
        }
        for label in COMMANDS
    }
    identical = all(all(v.values()) for v in same.values())
    doc = {
        "kind": "capture-surface",
        "prediction": "CAPS-P6",
        "run_at": _common.stamp(),
        "head": _common.git("rev-parse", "HEAD"),
        "baseline": {"rev": args.baseline_rev, "commit": baseline_head},
        "method": "bench/toolcost/run.py probe_tools (sandbox HOME, raw JSON-RPC); "
        "bytes and sha256 of the tools array serialized with sort_keys and "
        "compact separators (toolcost's full_bytes blob) and of the instructions "
        "string as UTF-8",
        "at_head": head,
        "at_baseline": base,
        "identical": same,
        "revision_2_tools_list_bytes": REVISION_2_TOOLS_LIST_BYTES,
        "head_matches_revision_2_length": head["shim"]["tools_list_bytes"]
        == REVISION_2_TOOLS_LIST_BYTES,
        "verdict": "HIT" if identical else "MISSED",
    }
    out = _common.write_result("surface.json", doc)
    print(f"wrote {out}")
    print(json.dumps({"head": head, "baseline": base, "verdict": doc["verdict"]}))


if __name__ == "__main__":
    main()
