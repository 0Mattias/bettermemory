"""`bettermemory backup create|verify|restore`: verified bundles of the
bettermemory 9 store (`bettermemory.backup` holds the mechanism and says
why each step is where it is).

`create` copies the live store through SQLite's online backup API, which is
safe while the daemon serves, into an owner-only bundle with its head
checkpoint and a signed manifest. `verify` checks a bundle on a temporary
copy and never opens its files in place. `restore` puts a bundle back over
the store this CLI resolves, with the daemon stopped, moving the current
files aside and rewinding the head checkpoint on the record, so the
restored store reads ``ok`` under `bettermemory log verify` instead of
``tampered`` for good.

None of the three opens the live store through `cli_context`, which creates
stores and rekeys: `create` reads it through a read-only SQLite connection,
`verify` never touches it, and `restore` opens it only after the swap, with
``allow_rekey=False``, to verify it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``backup`` and its ``create``, ``verify`` and ``restore``."""
    help_text = (
        "Verified backups of the bettermemory 9 store. `backup create` writes "
        "a consistent snapshot bundle while the daemon runs, `backup verify` "
        "checks a bundle's files, signature and log on a temporary copy, and "
        "`backup restore` puts one back with the daemon stopped, rewinding "
        "the head checkpoint on the record so `log verify` passes."
    )
    parser = sub.add_parser("backup", help=help_text, description=help_text)
    backup_sub = parser.add_subparsers(dest="backup_cmd")

    text = (
        "Write a bundle of the store: memory.sqlite (an online snapshot, "
        "safe while the daemon writes), the head checkpoint at the snapshot "
        "and a manifest signed with the store's key (version, store id, key "
        "fingerprints, head seq and MAC, row counts, a SHA-256 of every "
        "file). The bundle directory is 0700 and its files 0600. Without "
        "--include-key the keys stay out of the bundle and the manifest "
        "names the fingerprint they must match: keep a copy of the keys "
        "directory somewhere apart from the bundles. Exit 0 when the bundle "
        "is written, 1 when it could not be (--include-key with no key "
        "held, a write error), 2 when there is no store."
    )
    create = backup_sub.add_parser("create", help=text, description=text)
    create.add_argument(
        "--out",
        type=str,
        default=None,
        metavar="DIR",
        help=(
            "Write the bundle under DIR, as DIR/<UTC timestamp>/. DIR is "
            "created owner-only when missing and left as it is when it "
            "exists. Default: the backups directory beside the store file."
        ),
    )
    create.add_argument(
        "--include-key",
        action="store_true",
        help=(
            "Copy the store's keys into the bundle (0600), so it can be "
            "checked and restored on a machine without them (there it reads "
            "unverifiable, self-signed, and restores with --trust-bundle-key). "
            "Anyone holding such a bundle can sign rows as this store."
        ),
    )
    create.add_argument(
        "--json", action="store_true", help="Emit the result as JSON instead of text."
    )

    text = (
        "Check a bundle: every file's size and SHA-256 against the manifest, "
        "the manifest's signature, then the log (MACs, chain, segments, the "
        "head and the fold) on a temporary copy; the bundle itself is never "
        "opened in place. Keys come from the keys directory, else from the "
        "bundle when it carries them; a signature counts only under a key "
        "the keys directory holds, since anyone who can write a bundle can "
        "sign it with a key they put in it. Exit 0 when the bundle verifies, "
        "1 when it is tampered or does not match its manifest, 2 when it "
        "cannot be verified (a key is held nowhere, the manifest is unsigned "
        "or signed only by a key the bundle carries) or on a usage error."
    )
    verify = backup_sub.add_parser("verify", help=text, description=text)
    verify.add_argument("bundle", metavar="BUNDLE", help="The bundle directory.")
    verify.add_argument(
        "--keys-dir",
        type=str,
        default=None,
        metavar="DIR",
        help=(
            "Read keys the bundle does not carry from DIR instead of the "
            "store's keys directory (BETTERMEMORY_KEYS_DIR or the default)."
        ),
    )
    verify.add_argument(
        "--json", action="store_true", help="Emit the report as JSON instead of text."
    )

    text = (
        "Restore a bundle over the store this command resolves. Refuses "
        "while a daemon holds the store (close every session, run "
        "`bettermemory down`) and refuses a bundle that does not verify, "
        "including one signed only by a key it carries unless "
        "--trust-bundle-key. "
        "Moves the current store file and its -wal and -shm aside as "
        "<store>.pre-restore-<UTC timestamp> (never deleted), copies the "
        "bundle's database in, installs any key it needs from the bundle, "
        "rewinds the head checkpoint to the bundle's and records the rewind "
        "in <store_id>.backup_restore.json beside it, then runs log verify "
        "on the restored store. Without --yes prints the plan and changes "
        "nothing. Exit 0 when restored and the log verifies, 1 when refused "
        "or failed, 2 on a usage error, 3 when --yes was not given."
    )
    restore = backup_sub.add_parser("restore", help=text, description=text)
    restore.add_argument("bundle", metavar="BUNDLE", help="The bundle directory.")
    restore.add_argument(
        "--yes", action="store_true", help="Carry out the restore; without it, plan."
    )
    restore.add_argument(
        "--trust-bundle-key",
        action="store_true",
        help=(
            "Restore a bundle whose manifest is signed only by a key it "
            "carries (an --include-key bundle on a machine whose keys "
            "directory does not hold that key). The bundle's key becomes the "
            "store's current key, retiring any other, so use it only for a "
            "bundle kept where no one else can write."
        ),
    )
    restore.add_argument(
        "--json", action="store_true", help="Emit the result as JSON instead of text."
    )
    return parser


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory backup ...``."""
    command = args.backup_cmd
    if command is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if command == "create":
        _create(args, parser=sub_parser)
    elif command == "verify":
        _verify(args, parser=sub_parser)
    elif command == "restore":
        _restore(args, parser=sub_parser)
    else:
        sub_parser.error(f"unknown backup command: {command!r}")


def _store_file() -> Path:
    from ..config import STORE_FILENAME, load_config

    return load_config().resolved_directory() / STORE_FILENAME


def _bundle_dir(value: str, parser: argparse.ArgumentParser) -> Path:
    bundle = Path(value).expanduser()
    if not bundle.is_dir():
        parser.error(f"no bundle directory at {bundle}")
    return bundle


def _emit_json(document: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def _create(args: argparse.Namespace, *, parser: argparse.ArgumentParser) -> None:
    from ..backup import BackupError, create_backup

    store_file = _store_file()
    if not store_file.is_file():
        parser.error(f"no bettermemory 9 store at {store_file}")
    out = Path(args.out).expanduser() if args.out else None
    try:
        result = create_backup(store_file, out_dir=out, include_key=args.include_key)
    except BackupError as exc:
        sys.stderr.write(f"bettermemory backup create: {exc}\n")
        raise SystemExit(1) from None
    for warning in result["warnings"]:
        sys.stderr.write(f"bettermemory backup create: warning: {warning}\n")
    if args.json:
        _emit_json(result)
    else:
        sys.stdout.write(render_create(result))
    raise SystemExit(0)


def render_create(result: dict[str, Any]) -> str:
    """The text form of a `create_backup` result."""
    manifest = result["manifest"]
    counts = manifest["counts"]
    fingerprint = str(manifest["key_fingerprint"])
    lines = [
        f"bettermemory backup create: wrote {result['bundle']}",
        f"  store {manifest['source']}",
        f"  id {manifest['store_id']}",
        f"  head seq {manifest['head']['seq']}, {counts.get('log', 0)} log rows, "
        f"{counts.get('memories', 0)} memories, "
        f"{counts.get('tombstones', 0)} tombstones, "
        f"{counts.get('episodes', 0)} episodes",
    ]
    if manifest["key_included"]:
        lines.append(
            f"  key {fingerprint[:16]} INCLUDED: anyone holding this bundle can "
            "sign rows as this store; keep it where only you can read it"
        )
    else:
        lines.append(
            f"  key {fingerprint[:16]} not included: verifying or restoring "
            "needs it from the keys directory; back that up apart from the bundles"
        )
    if "mac" not in manifest:
        lines.append("  manifest NOT signed: the store's key is not held here")
    lines.append(f"  check it with: bettermemory backup verify {result['bundle']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _verify(args: argparse.Namespace, *, parser: argparse.ArgumentParser) -> None:
    from ..backup import VERIFY_EXIT, verify_bundle

    bundle = _bundle_dir(args.bundle, parser)
    keys_dir = Path(args.keys_dir).expanduser() if args.keys_dir else None
    report = verify_bundle(bundle, keys_dir=keys_dir)
    if args.json:
        _emit_json(report)
    else:
        sys.stdout.write(render_verify(report))
    raise SystemExit(VERIFY_EXIT[report["status"]])


def _problem_line(problem: dict[str, Any]) -> str:
    extra = {k: v for k, v in problem.items() if k != "problem"}
    if "seq" in extra:
        return f"seq {extra['seq']} ({extra.get('kind', '')}): {problem['problem']}"
    if not extra:
        return str(problem["problem"])
    detail = ", ".join(f"{k} {v}" for k, v in sorted(extra.items()))
    return f"{problem['problem']}: {detail}"


def render_verify(report: dict[str, Any], *, title: str = "backup verify") -> str:
    """The text form of a `verify_bundle` report."""
    lines = [
        f"bettermemory {title}: {report['status']}",
        f"  bundle {report['bundle']}",
    ]
    if report["store_id"] is not None:
        head = report["head"] or {}
        lines.append(
            f"  store {report['store_id']}, created {report['created_at']}, "
            f"head seq {head.get('seq')}"
        )
    if report["files"]:
        lines.append(
            "  files "
            + ", ".join(f"{f['name']} {f['status']}" for f in report["files"])
        )
    if report["manifest_mac"] == "self_signed":
        lines.append(
            "  manifest signature self_signed: made with a key only the bundle "
            "carries, which proves the bundle whole but not who made it"
        )
    elif report["manifest_mac"] is not None:
        lines.append(f"  manifest signature {report['manifest_mac']}")
    for key in report["keys"]:
        source = key["source"] or "held nowhere"
        where = {"bundle": "from the bundle", "keys_dir": "from the keys directory"}
        lines.append(f"  key {key['fingerprint'][:16]} {where.get(source, source)}")
    log = report["log"]
    if log is not None:
        lines.append(
            f"  log {log['status']}: {log['rows']} rows in "
            f"{len(log['segments'])} segment(s), head {log['head']['status']}, "
            f"fold {log['fold']['status']}"
        )
    problems = list(report["problems"])
    if log is not None:
        problems += list(log["problems"])
        unaccounted = log.get("unaccounted_ids") or []
        problems += [{"problem": "unaccounted", "id": rid} for rid in unaccounted]
    if problems:
        lines.append("  problems")
        lines.extend(f"    {_problem_line(p)}" for p in problems)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def _restore(args: argparse.Namespace, *, parser: argparse.ArgumentParser) -> None:
    from ..backup import RESTORE_EXIT, restore_bundle

    bundle = _bundle_dir(args.bundle, parser)
    report = restore_bundle(
        bundle,
        _store_file(),
        apply=args.yes,
        trust_bundle_key=args.trust_bundle_key,
    )
    if args.json:
        _emit_json(report)
    else:
        sys.stdout.write(render_restore(report))
    raise SystemExit(RESTORE_EXIT[report["status"]])


def _head_text(head: dict[str, Any] | None) -> str:
    return "none" if head is None else f"seq {head['seq']}"


def render_restore(report: dict[str, Any]) -> str:
    """The text form of a `restore_bundle` report."""
    status = report["status"]
    heading = {
        "planned": "plan (nothing changed; run again with --yes to restore)",
    }.get(status, status)
    lines = [
        f"bettermemory backup restore: {heading}",
        f"  bundle {report['bundle']}",
        f"  store {report['store']}",
    ]
    if report["detail"]:
        lines.append(f"  {report['detail']}")
    verification = report["verify"]
    if verification is not None and verification["status"] != "ok":
        lines.append(render_verify(verification).rstrip("\n").replace("\n", "\n  "))
    plan = report["plan"]
    if plan is not None:
        current = plan["current_store_id"]
        if current is None:
            lines.append("  current store: none readable at the path")
        elif current == plan["bundle_store_id"]:
            lines.append(f"  current store {current}, the bundle's own")
        else:
            lines.append(
                f"  current store {current}; the bundle is a different store, "
                f"{plan['bundle_store_id']}"
            )
        if plan.get("trusted_bundle_key"):
            lines.append(
                f"  trusting the bundle's own key {plan['trusted_bundle_key'][:16]} "
                "(--trust-bundle-key)"
            )
        # A plan says what would happen; a restore that ran, finished or
        # not, says what did, so a failure part way never reads as a key
        # installed or a head rewound that was not.
        done = status != "planned"
        moves = report["moved_aside"] if done else plan["move_aside"]
        verb = "moved aside" if done else "move aside"
        for move in moves:
            lines.append(f"  {verb}: {move['from']} -> {move['to']}")
        for action in report["keys_installed"] if done else plan["keys"]:
            what = (
                "current key" if action["action"] == "make_current" else "retired key"
            )
            retires = (
                f", retiring {action['retires'][:16]}" if action["retires"] else ""
            )
            lines.append(
                f"  key {action['fingerprint'][:16]}: "
                f"{'installed' if done else 'install'} as the {what} from "
                f"the {action['source'].replace('_', ' ')}{retires}"
            )
        if done and report["head"] is None:
            lines.append(
                f"  head checkpoint: {_head_text(plan['head']['before'])}, not rewound"
            )
        else:
            head = report["head"] or plan["head"]
            record = report["record"] if done else plan["record"]
            lines.append(
                f"  head checkpoint: {_head_text(head['before'])} -> "
                f"{_head_text(head['after'])}, "
                + (f"recorded in {record}" if record else "the rewind not recorded")
            )
    log = report["log"]
    if log is not None:
        lines.append(f"  log verify of the restored store: {log['status']}")
    return "\n".join(lines) + "\n"
