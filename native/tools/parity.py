"""Run the native build against the Python reference on this machine (unit PAR).

The CI job `native parity` runs this script and nothing else, so a local run
is the job. Run it from the repository root with the reference Python (the
project environment's), e.g. ``uv run python native/tools/parity.py --lint``.

Steps:

0. Lint (with ``--lint``): ``cargo build --locked``, ``cargo clippy --locked
   --all-targets -- -D warnings``, ``cargo fmt --check``.
1. ``cargo test --locked`` against the committed golden files. They were made
   on darwin-arm64 under Python 3.13.13, SQLite 3.50.4, Unicode 15.1.0; on
   another platform the score-bearing cases that differ are set aside, named
   and counted (class iii), and everything else is compared strictly.
2. ``python -m tests.contract.generate --out DIR --as-found`` with this
   Python, then ``cargo test --locked`` with ``BM_GOLDEN_DIR=DIR`` and
   ``BM_REFERENCE_PYTHON`` at this Python: the parity claim on this
   platform. Here a class (iii) case is never set aside (the set was made
   here); a class (ii) case is set aside only where this Python or its
   Unicode is not the one the port reproduces.

It prints the manifest and a table of every gated test's counts, appends
them to ``$GITHUB_STEP_SUMMARY`` when that is set, and exits non-zero when
step 0, 1 or 2 fails, a generator raised, or step 2 set aside a class (i) or
(iii) case. A class (os) case is an expectation the operating system cannot
meet (Windows cannot remove an open file and has no mode bits); the test
checks what holds there instead, and the case is named and counted, never
failed.

``--golden DIR`` skips the generation and uses DIR (made earlier with
``--as-found``): how a perturbed or foreign set is tried locally.
``BM_PLATFORM_KEY`` makes both sides report another platform.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

Record = dict[str, Any]

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "native"


def run(
    title: str, cmd: list[str], cwd: Path, env: dict[str, str]
) -> tuple[int, list[str]]:
    """Runs ``cmd``, streaming its output, and returns its status and lines."""
    print(f"\n=== {title}: {' '.join(cmd)}", flush=True)
    lines: list[str] = []
    with subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            lines.append(line.rstrip("\n"))
        status = proc.wait()
    print(f"=== {title}: exit {status}", flush=True)
    return status, lines


def tally(lines: list[str]) -> tuple[int, int, int]:
    """passed, failed, ignored over every `test result:` line."""
    passed = failed = ignored = 0
    for line in lines:
        if line.startswith("test result:"):
            words = line.replace(";", "").split()
            for i, word in enumerate(words):
                if word == "passed":
                    passed += int(words[i - 1])
                elif word == "failed":
                    failed += int(words[i - 1])
                elif word == "ignored":
                    ignored += int(words[i - 1])
    return passed, failed, ignored


def read_log(path: Path) -> dict[tuple[str, str], Record]:
    out: dict[tuple[str, str], Record] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                out[(record["test"], record["file"])] = record
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="native/tools/parity.py")
    parser.add_argument("--lint", action="store_true", help="build, clippy, fmt first")
    parser.add_argument(
        "--work", type=Path, help="scratch directory (default: a new one)"
    )
    parser.add_argument(
        "--golden", type=Path, help="use this as-found set, do not generate"
    )
    parser.add_argument(
        "--skip-committed", action="store_true", help="skip step 1 (local experiments)"
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    from tests.contract.generate import platform_key, python_version

    work = args.work or Path(
        tempfile.mkdtemp(prefix="native-parity-", dir=os.environ.get("RUNNER_TEMP"))
    )
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("BM_GOLDEN_DIR", None)
    env.setdefault("CARGO_TERM_COLOR", "never")
    failures: list[str] = []

    for tool in (["rustc", "-Vv"], ["cargo", "-V"]):
        run(tool[0], tool, NATIVE, env)
    print(
        f"reference Python {python_version()} at {sys.executable}, "
        f"platform {platform_key()}"
    )

    if args.lint:
        for title, cmd in [
            ("build", ["cargo", "build", "--locked"]),
            (
                "clippy",
                [
                    "cargo",
                    "clippy",
                    "--locked",
                    "--all-targets",
                    "--",
                    "-D",
                    "warnings",
                ],
            ),
            ("fmt", ["cargo", "fmt", "--check"]),
        ]:
            status, _ = run(title, cmd, NATIVE, env)
            if status:
                failures.append(f"{title} failed (exit {status})")

    step1_log = work / "step1.jsonl"
    step1 = (0, 0, 0)
    if not args.skip_committed:
        step1_log.unlink(missing_ok=True)
        status, lines = run(
            "step 1: cargo test against the committed files",
            ["cargo", "test", "--locked", "--no-fail-fast"],
            NATIVE,
            {**env, "BM_PARITY_LOG": str(step1_log)},
        )
        step1 = tally(lines)
        if status:
            failures.append(f"step 1 (committed files) failed (exit {status})")

    golden = args.golden
    if golden is None:
        golden = work / "golden"
        status, _ = run(
            "step 2a: regenerate the fixtures as found",
            [
                sys.executable,
                "-m",
                "tests.contract.generate",
                "--out",
                str(golden),
                "--as-found",
            ],
            ROOT,
            env,
        )
        if status:
            failures.append(f"a generator raised under this platform (exit {status})")
    try:
        manifest = json.loads((golden / "platform.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        failures.append(f"no readable platform.json in the set under test: {exc}")
        manifest = {
            "platform": "?",
            "python": "?",
            "sqlite": "?",
            "unicode": "?",
            "machine": "?",
            "committed": {},
            "raised": [],
            "files": {},
            "generators": {},
        }

    step2_log = work / "step2.jsonl"
    step2_log.unlink(missing_ok=True)
    status, lines = run(
        "step 2b: cargo test against the fixtures made here",
        ["cargo", "test", "--locked", "--no-fail-fast"],
        NATIVE,
        {
            **env,
            "BM_GOLDEN_DIR": str(golden.resolve()),
            "BM_REFERENCE_PYTHON": sys.executable,
            "BM_PARITY_LOG": str(step2_log),
        },
    )
    step2 = tally(lines)
    if status:
        failures.append(f"step 2 (fixtures made here) failed (exit {status})")

    s1, s2 = read_log(step1_log), read_log(step2_log)
    for (test, file), record in sorted(s2.items()):
        if record["set_aside"] and record["class"] in ("i", "iii"):
            failures.append(
                f"step 2 set aside {record['set_aside']} class ({record['class']}) "
                f"case(s) of {test} [{file}]: {record['why']}"
            )
    if manifest["platform"] != platform_key():
        failures.append(
            f"the set under test was made on {manifest['platform']}, "
            f"this is {platform_key()}: step 2 is no parity claim"
        )

    report = summary(manifest, s1, s2, step1, step2, failures, args.skip_committed)
    print("\n" + report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report + "\n")
    return 1 if failures else 0


def cell(record: Record | None) -> str:
    if record is None:
        return "-"
    text = f"{record['compared']} compared"
    if record["set_aside"]:
        text += f", **{record['set_aside']} set aside**"
    return text


def summary(
    manifest: Record,
    s1: dict[tuple[str, str], Record],
    s2: dict[tuple[str, str], Record],
    step1: tuple[int, int, int],
    step2: tuple[int, int, int],
    failures: list[str],
    skipped1: bool,
) -> str:
    out = [
        f"## native parity: {manifest['platform']}",
        "",
        f"Reference: Python {manifest['python']}, SQLite {manifest['sqlite']}, "
        f"Unicode {manifest['unicode']}, machine {manifest['machine']}, "
        f"libc {manifest.get('libc')}; committed files: "
        + ", ".join(f"{k} {v}" for k, v in sorted(manifest["committed"].items())),
        "",
        "- step 1 (committed files): "
        + (
            "skipped" if skipped1 else "{} passed, {} failed, {} ignored".format(*step1)
        ),
        "- step 2 (fixtures made here): {} passed, {} failed, {} ignored".format(
            *step2
        ),
        f"- generators that raised: {', '.join(manifest['raised']) or 'none'}",
        "",
        "| test | file | class | step 1: committed | step 2: made here | why set aside |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for key in sorted(set(s1) | set(s2)):
        a, b = s1.get(key), s2.get(key)
        why = "; ".join(
            f"step {n}: {r['why']}" for n, r in ((1, a), (2, b)) if r and r["set_aside"]
        )
        out.append(
            f"| {key[0]} | {key[1]} | {(a or b or {}).get('class')} | {cell(a)} | {cell(b)} | {why} |"
        )
    for n, log in ((1, s1), (2, s2)):
        for (test, file), record in sorted(log.items()):
            if record["set_aside"]:
                out += [
                    "",
                    f"Step {n}, {test} [{file}], set aside: {record['why']}",
                    "```",
                ]
                out += [str(case)[:600] for case in record["set_aside_cases"]]
                if record["set_aside"] > len(record["set_aside_cases"]):
                    out.append(
                        f"... and {record['set_aside'] - len(record['set_aside_cases'])} more"
                    )
                out.append("```")
    differing = {
        name: info for name, info in manifest["files"].items() if not info["identical"]
    }
    out += [
        "",
        f"Files made here that differ from the committed ones: {len(differing)}",
    ]
    for name, info in sorted(differing.items()):
        out.append(f"- {name} ({info['generator']}): {info.get('differs')}")
    for name, entry in sorted(manifest["generators"].items()):
        if "raised" in entry:
            out.append(f"- generator {name} raised: {entry['raised']}")
        if "not_here" in entry:
            out.append(f"- generator {name} not run here: {entry['not_here']}")
    out += ["", "Verdict: " + ("FAIL" if failures else "pass")]
    out += [f"- {f}" for f in failures]
    out += ["", "<details><summary>platform.json</summary>", "", "```json"]
    out += [json.dumps(manifest, indent=2, sort_keys=True), "```", "</details>"]
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
