"""The container's half of the membership-docker leg (design 8.1 and 8.7,
E10): the steps of the hook client's witness exchange
(`_daemon_client._membership_rounds`), one step per call, run inside a
container started with ``--network none``. The host side
(tests/test_membership_container.py) posts what a step prints to a daemon
on the host and hands the daemon's next question to the next step, so the
daemon never reads a path of the container and the container opens no
socket.

A command reads its input as one JSON object on stdin where it takes any,
and prints one JSON object on stdout:

- ``probe CWD``: the namespace probe (`location.probe`).
- ``capture CWD POLICY``: the location token of CWD (`location.capture`,
  writing the checkout token where POLICY is 1, decision D11) and this
  container's machine (`machine.open_machine`).
- ``answer``: the answers to the daemon's question (`location.answer`)
  from ``{"question", "answers", "location"}``.
- ``remint``: the checkout token re-minted (`checkout_token.remint`) for
  the checkout of ``{"location", "policy"}``.
- ``rc3``: what an rc3 hook client posts from the working directory: its
  cwd and no location.
- ``facts PATH``: what the leg exercised: the network interfaces, the
  user, the hostname, the mount PATH is on, and PATH's device, inode and
  birth time (None where statx reports none).

The package is imported from the source tree the container mounts
(PYTHONPATH); only the hook client's standard-library modules load.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

from bettermemory import checkout_token, location
from bettermemory.machine import open_machine


def _stdin() -> dict[str, Any]:
    raw = json.load(sys.stdin)
    if not isinstance(raw, dict):
        raise SystemExit("container_client: expected one JSON object on stdin")
    return raw


def _capture_of(raw: Any) -> location.Capture:
    cap = location.Capture.from_token(raw)
    if cap is None:
        raise SystemExit("container_client: not a location token of this version")
    return cap


def _mount_of(path: str) -> str | None:
    """The /proc/mounts line of the mount `path` is on: the one whose
    mount point is the longest prefix of the path."""
    real = os.path.realpath(path)
    best: tuple[int, str] | None = None
    with open("/proc/mounts", encoding="utf-8") as fh:
        for line in fh:
            fields = line.split()
            if len(fields) < 3:
                continue
            point = fields[1]
            inside = real == point or real.startswith(point.rstrip("/") + "/")
            if inside and (best is None or len(point) >= best[0]):
                best = (len(point), line.strip())
    return None if best is None else best[1]


def _facts(path: str) -> dict[str, Any]:
    st = os.stat(path)
    key = location.object_key(path, st)
    return {
        "interfaces": sorted(os.listdir("/sys/class/net")),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "hostname": socket.gethostname(),
        "python": sys.version.split()[0],
        "mount": _mount_of(path),
        "device": st.st_dev,
        "inode": st.st_ino,
        "birth_ns": None if key is None else key[2],
    }


def main(argv: list[str]) -> dict[str, Any]:
    if not argv:
        raise SystemExit("usage: container_client.py COMMAND [ARGS]")
    command, args = argv[0], argv[1:]
    if command == "probe" and len(args) == 1:
        return location.probe(args[0])
    if command == "capture" and len(args) == 2:
        cap = location.capture(args[0], write_token=args[1] == "1")
        return {
            "location": cap.to_token(),
            "machine": location.identity_to_json(open_machine()),
        }
    if command == "answer" and not args:
        given = _stdin()
        question = given.get("question")
        answers = given.get("answers")
        return location.answer(
            question if isinstance(question, dict) else {},
            answers if isinstance(answers, dict) else {},
            _capture_of(given.get("location")),
        )
    if command == "remint" and not args:
        given = _stdin()
        cap = _capture_of(given.get("location"))
        if not cap.git_dir:
            raise SystemExit("container_client: the location has no git directory")
        wrote = checkout_token.remint(
            cap.git_dir,
            policy=bool(given.get("policy")),
            bare=cap.kind == location.BARE,
        )
        return {"status": wrote.status, "token": wrote.token}
    if command == "rc3" and not args:
        return {"cwd": os.getcwd()}
    if command == "facts" and len(args) == 1:
        return _facts(args[0])
    raise SystemExit(f"container_client: unknown command {argv!r}")


if __name__ == "__main__":
    json.dump(main(sys.argv[1:]), sys.stdout)
    sys.stdout.write("\n")
