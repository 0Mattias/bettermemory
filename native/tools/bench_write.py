"""Throughput of the reference's record write at the native unit's level:
5,000 ``memory_put``s through ``Store._mutate`` in a transaction each
(``Store._transaction``, the head moved after each commit), the same
payloads as ``store_write_robustness.rs``'s ``bench_five_thousand_puts``,
on a store ``Store.create`` makes in a temporary directory. Prints the
records per second."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from bettermemory.store import Store

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(n: int) -> str:
    tail = ""
    for _ in range(23):
        tail = CROCKFORD[n % 32] + tail
        n //= 32
    return "01J" + tail


def payload(k: int) -> dict[str, object]:
    return {
        "memory": {
            "id": ulid(k),
            "created": "2001-02-03T04:05:06.000000Z",
            "updated": "2001-02-03T04:05:06.000000Z",
            "scopes": ["global", "bench"],
            "confidence": "high",
            "source": "explicit-statement",
            "body": f"benchmark record {k} about sqlite docker and staging",
            "links": [],
        },
        "provenance": "local",
        "filename": None,
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        store = Store.create(root, keys_dir=root / "keys")
        payloads = [payload(k) for k in range(5000)]
        start = time.perf_counter()
        for p in payloads:
            with store._transaction() as tx:
                store._mutate(tx, "memory_put", p, session="bench")
        secs = time.perf_counter() - start
        store.close()
    print(f"reference: 5000 puts in {secs:.3f} s, {5000 / secs:.0f} per second")
    return 0


if __name__ == "__main__":
    sys.exit(main())
