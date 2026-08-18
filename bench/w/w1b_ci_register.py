"""Derive the committed SE-register slice for W1b's CI determinism check.

G3 of `bench/w/W1B_DECLARATION.md` §7 extends the CI leg with a second
reduced register derived from the pinned Stack Exchange bytes, so the
determinism assert exercises the register this unit actually turns on.
The derivation replays from the pin: post documents of the beer
archive — the smallest admitted site — through the W1b reader
(`w1b_corpus.iter_se_docs`), in document order, blank-line separated,
truncated at exactly the byte budget (safely under the declaration's
2 MB cap). The output lands at ``bench/w/ci_register/reduced_se.txt``
and is pinned in ``bench/w/corpora.json`` as a derived entry with its
own sha256. Stack Exchange content is CC BY-SA, so the derived slice
is committable with attribution carried by the register entry.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from w1b_corpus import REGISTER, iter_se_docs

_BYTE_BUDGET = 1_000_000


def main() -> int:
    register = json.loads(REGISTER.read_text())
    by_name = {c["name"]: c for c in register["corpora"]}
    root = REGISTER.parent.parent.parent
    archive = root / str(by_name["beer-stackexchange-archive"]["local_path"])
    parts: list[str] = []
    size = 0
    for doc in iter_se_docs(archive):
        chunk = doc + "\n\n"
        data = chunk.encode("utf-8")
        if size + len(data) >= _BYTE_BUDGET:
            keep = data[: _BYTE_BUDGET - size]
            parts.append(keep.decode("utf-8", errors="ignore"))
            break
        parts.append(chunk)
        size += len(data)
    out = Path(__file__).parent / "ci_register" / "reduced_se.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text("".join(parts), encoding="utf-8")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"{out} {out.stat().st_size} bytes sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
