"""Run the native build against the Python reference on this machine (unit PAR).

The CI jobs `native parity (<os>, K/N)` and `native lint (<os>)` run only
this script, and a local run with --lint and no --shard covers all of them.
Run it from the repository root with the reference Python (the project
environment's), e.g.
``uv run python native/tools/parity.py --lint``.

Steps:

0. Lint (with ``--lint``): ``cargo build --locked``, ``cargo clippy --locked
   --all-targets -- -D warnings``, then ``rustfmt --check`` over the root
   file of every workspace target in batches (``fmt_check``): ``cargo fmt
   --check`` puts every root on one command line, which Windows refuses.
1. ``cargo test --locked`` against the committed golden files. They were made
   on darwin-arm64 under Python 3.13.13, SQLite 3.50.4, Unicode 15.1.0; on
   another platform the score-bearing cases that differ are set aside, named
   and counted (class iii), and everything else is compared strictly. Then
   ``cargo test --locked --release`` of the test binaries in
   ``RELEASE_TESTS``, whose debug build reads a part of what their release
   build reads (step 1b, counted with step 1).
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

``--shard K/N`` runs steps 1, 1b, 2a and 2b over one part of the test
targets: the list ``shard_items`` makes (the unit, example and doc tests of
every library first, then each integration test target, sorted by package
and name) split by index modulo N, so item i is in shard ``i % N + 1``.
A shard names its targets to cargo (``--workspace --test NAME ...``, in
batches under ``FMT_ARG_BOUND``); the unit, example and doc tests run in
shard 1 only, a binary of ``RELEASE_TESTS`` in step 1b of the shard its
target is in. Step 2a makes the whole set in every shard, so the manifest
the gates open by is the one an unsharded run reads. A shard that selects
nothing fails. Without ``--shard`` every call is the one it always was.
``--list`` prints the selection and the cargo calls and runs nothing;
``--lint-only`` runs step 0 alone. The CI jobs `native parity (<os>,
K/N)` run one shard each and `native lint (<os>)` runs ``--lint-only``.
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

#: Test binaries step 1b runs again in a release build: a debug build of
#: each asks a part of what its release build asks. ``golden_pyre_fold``
#: asks `Lit::matches` of the candidate code points in a debug build and of
#: every code point in a release build (native/README.md, "Unit 1, built").
RELEASE_TESTS = ("golden_pyre_fold",)

#: The item of the shard list that stands for every library's unit tests,
#: every example and every doc test: what a plain `cargo test` runs besides
#: the integration test targets. It is item 0, so shard 1 runs it.
UNIT_ITEM = "(unit, example and doc tests)"


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


#: The most characters of file arguments one rustfmt call is given. Windows
#: refuses a command line over 32,767 characters (os error 206), and `cargo
#: fmt --check` passes the root file of every target, several hundred
#: integration test files among them, on one line; this bound stays far
#: under that limit with room for the program name and flags.
FMT_ARG_BOUND = 8000


def fmt_batches(paths: list[str], bound: int = FMT_ARG_BOUND) -> list[list[str]]:
    """Splits ``paths``, in order, into batches whose joined length (one
    separator per argument) is at most ``bound``. A path longer than the
    bound alone forms a batch of its own; every path is in exactly one batch."""
    out: list[list[str]] = []
    batch: list[str] = []
    size = 0
    for path in paths:
        cost = len(path) + 1
        if batch and size + cost > bound:
            out.append(batch)
            batch, size = [], 0
        batch.append(path)
        size += cost
    if batch:
        out.append(batch)
    return out


def fmt_roots(native: Path, env: dict[str, str]) -> dict[str, list[str]]:
    """The root file of every target of every workspace member, grouped by
    the target's edition, as paths relative to ``native``: the files `cargo
    fmt` hands rustfmt. rustfmt reaches the rest through `mod` items."""
    meta = cargo_metadata(native, env)
    members = set(meta["workspace_members"])
    roots: dict[str, set[str]] = {}
    for package in meta["packages"]:
        if package["id"] not in members:
            continue
        for target in package["targets"]:
            path = Path(target["src_path"]).resolve()
            rel = path.relative_to(native.resolve()).as_posix()
            roots.setdefault(target["edition"], set()).add(rel)
    return {edition: sorted(files) for edition, files in sorted(roots.items())}


def fmt_check(native: Path, env: dict[str, str]) -> int:
    """`cargo fmt --check` without one command line of every file: runs
    ``rustfmt --check`` over the target root files in batches under
    ``FMT_ARG_BOUND``. Prints the first differing file; returns 1 when any
    batch fails (rustfmt's own status otherwise)."""
    print("\n=== fmt: rustfmt --check over every target root, in batches", flush=True)
    status = 0
    first_diff: str | None = None
    count = 0
    for edition, files in fmt_roots(native, env).items():
        count += len(files)
        for batch in fmt_batches(files):
            code, lines = run(
                f"fmt (edition {edition}, {len(batch)} files)",
                ["rustfmt", "--check", "--edition", edition, *batch],
                native,
                env,
            )
            if code:
                status = status or code
                if first_diff is None:
                    first_diff = next(
                        (line for line in lines if line.startswith("Diff in ")),
                        f"rustfmt exit {code} on a batch starting {batch[0]}",
                    )
    if first_diff is not None:
        print(f"first difference: {first_diff}", flush=True)
    print(f"=== fmt: {count} root files, exit {status}", flush=True)
    return status


def parse_shard(text: str) -> tuple[int, int]:
    """``K/N`` with 1 <= K <= N."""
    try:
        k, n = (int(part) for part in text.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not K/N: {text!r}") from None
    if not 1 <= k <= n:
        raise argparse.ArgumentTypeError(f"not 1 <= K <= N: {text!r}")
    return k, n


def test_targets(meta: Record) -> list[str]:
    """The integration test targets cargo runs in a plain `cargo test` of
    the workspace (kind ``test``, ``test`` not off), sorted by package and
    name. Names are unique across the workspace, since a shard names them
    with ``--workspace --test NAME``."""
    members = set(meta["workspace_members"])
    found: list[tuple[str, str]] = []
    for package in meta["packages"]:
        if package["id"] not in members:
            continue
        for target in package["targets"]:
            if "test" in target["kind"] and target.get("test", True):
                found.append((package["name"], target["name"]))
    names = [name for _, name in sorted(found)]
    if len(set(names)) != len(names):
        raise SystemExit("two workspace packages have a test target of one name")
    return names


def shard_items(targets: list[str]) -> list[str]:
    """The list shards split: ``UNIT_ITEM``, then the targets in order."""
    return [UNIT_ITEM, *targets]


def shard_select(items: list[str], k: int, n: int) -> list[str]:
    """Shard ``k`` of ``n``: the items whose index modulo ``n`` is ``k - 1``."""
    return [item for i, item in enumerate(items) if i % n == k - 1]


def test_batches(names: list[str], bound: int = FMT_ARG_BOUND) -> list[list[str]]:
    """``--test NAME`` arguments for ``names``, in order, in batches whose
    joined length (one separator per argument) is at most ``bound``."""
    return [
        [arg for pair in batch for arg in pair.split(" ", 1)]
        for batch in fmt_batches([f"--test {name}" for name in names], bound)
    ]


def test_calls(selection: list[str] | None, release: bool = False) -> list[list[str]]:
    """The cargo calls of step 1 or 2b (``release`` False) or step 1b over
    ``selection`` (``None``: no shard, the calls of an unsharded run)."""
    base = ["cargo", "test", "--locked"]
    if release:
        base = [*base, "--release", "--no-fail-fast"]
        names = [
            name for name in RELEASE_TESTS if selection is None or name in selection
        ]
        if not names:
            return []
        return [[*base, *(arg for name in names for arg in ("--test", name))]]
    base.append("--no-fail-fast")
    if selection is None:
        return [base]
    calls: list[list[str]] = []
    if UNIT_ITEM in selection:
        calls.append([*base, "--workspace", "--lib", "--bins", "--examples"])
        calls.append([*base, "--workspace", "--doc"])
    names = [item for item in selection if item != UNIT_ITEM]
    calls += [[*base, "--workspace", *batch] for batch in test_batches(names)]
    return calls


def cargo_metadata(native: Path, env: dict[str, str]) -> Record:
    meta: Record = json.loads(
        subprocess.run(
            ["cargo", "metadata", "--no-deps", "--format-version", "1", "--locked"],
            cwd=native,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    )
    return meta


def run_calls(
    title: str, calls: list[list[str]], env: dict[str, str]
) -> tuple[int, tuple[int, int, int]]:
    """Runs every call (each one, whatever the one before gave); returns the
    first non-zero status and the summed tally."""
    status = 0
    total = (0, 0, 0)
    for i, cmd in enumerate(calls, 1):
        part = title if len(calls) == 1 else f"{title} (call {i} of {len(calls)})"
        code, lines = run(part, cmd, NATIVE, env)
        status = status or code
        passed, failed, ignored = tally(lines)
        total = (total[0] + passed, total[1] + failed, total[2] + ignored)
    return status, total


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
    parser.add_argument(
        "--shard",
        type=parse_shard,
        metavar="K/N",
        help="run steps 1, 1b, 2a and 2b over shard K of N of the test targets",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the selection and the cargo calls, run nothing",
    )
    parser.add_argument(
        "--lint-only", action="store_true", help="run step 0 (lint) alone"
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

    selection: list[str] | None = None
    shard_label = ""
    if args.shard is not None:
        k, n = args.shard
        shard_label = f" (shard {k}/{n})"
        items = shard_items(test_targets(cargo_metadata(NATIVE, env)))
        selection = shard_select(items, k, n)
    if args.list:
        if selection is not None:
            print(
                f"shard {args.shard[0]}/{args.shard[1]}: {len(selection)} of {len(items)} items"
            )
            for item in selection:
                print(f"  {item}")
        for title, calls in (
            ("step 1 and step 2b", test_calls(selection)),
            ("step 1b", test_calls(selection, release=True)),
        ):
            print(f"{title}: {len(calls)} call(s)")
            for cmd in calls:
                print("  " + " ".join(cmd))
        return 0

    for tool in (["rustc", "-Vv"], ["cargo", "-V"]):
        run(tool[0], tool, NATIVE, env)
    print(
        f"reference Python {python_version()} at {sys.executable}, "
        f"platform {platform_key()}"
    )

    if args.lint or args.lint_only:
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
        ]:
            status, _ = run(title, cmd, NATIVE, env)
            if status:
                failures.append(f"{title} failed (exit {status})")
        status = fmt_check(NATIVE, env)
        if status:
            failures.append(f"fmt failed (exit {status})")
    if args.lint_only:
        print("\nVerdict: " + ("FAIL" if failures else "pass"))
        for failure in failures:
            print(f"- {failure}")
        return 1 if failures else 0
    if selection is not None and not selection:
        failures.append(f"shard{shard_label} selects no test target")

    step1_log = work / "step1.jsonl"
    step1 = (0, 0, 0)
    if not args.skip_committed:
        step1_log.unlink(missing_ok=True)
        status, step1 = run_calls(
            "step 1: cargo test against the committed files",
            test_calls(selection),
            {**env, "BM_PARITY_LOG": str(step1_log)},
        )
        if status:
            failures.append(f"step 1 (committed files) failed (exit {status})")
        release = test_calls(selection, release=True)
        if not release:
            print("\n=== step 1b: no binary of RELEASE_TESTS in this shard", flush=True)
        status, (passed, failed, ignored) = run_calls(
            "step 1b: the same files, a release build of the tests that read "
            "every case only there",
            release,
            env,
        )
        step1 = (step1[0] + passed, step1[1] + failed, step1[2] + ignored)
        if status:
            failures.append(f"step 1b (a release build) failed (exit {status})")

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
    status, step2 = run_calls(
        "step 2b: cargo test against the fixtures made here",
        test_calls(selection),
        {
            **env,
            "BM_GOLDEN_DIR": str(golden.resolve()),
            "BM_REFERENCE_PYTHON": sys.executable,
            "BM_PARITY_LOG": str(step2_log),
        },
    )
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

    report = summary(
        manifest, s1, s2, step1, step2, failures, args.skip_committed, shard_label
    )
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
    shard_label: str = "",
) -> str:
    out = [
        f"## native parity: {manifest['platform']}{shard_label}",
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
