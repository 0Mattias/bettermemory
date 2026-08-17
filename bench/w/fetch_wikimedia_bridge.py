"""Fetch and pin the two Wikimedia bridge corpora W3's successors need.

Under the owner's broadened go-ahead of 2026-08-17 ("I am giving you
the go ahead to download"), announced with names and sizes before the
fetch ran. Two corpora, one purpose each:

- **enwiktionary pages-articles** (eight part files of the dated
  20260801 dump): explicit synonym / related-terms / hypernym
  relations in everyday English — the non-neural bridge source for
  relation classes that duplicate-question pairs structurally cannot
  carry (the anatomy's guitar↔Gibson need is an instance-of edge, not
  a paraphrase).
- **simplewiki pages-articles** (single multistream file, same dump
  date): plain-register definitional sentences ("X is a Y") for
  hypernym extraction with less markup noise than full enwiki.

Expected sizes and publisher sha1s below were read from each dump's
dumpstatus.json on 2026-08-17. A file that fails its published sha1
is deleted and not registered; the sha256 over the exact fetched
bytes is the register's authoritative pin (the enwiki-parts
precedent). Both entries carry the standing guard: no unit reads
these bytes until its declaration admits them.

Run: fastvenv/bin/python bench/w/fetch_wikimedia_bridge.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REGISTER = REPO / "bench" / "w" / "corpora.json"

WIKT_BASE = "https://dumps.wikimedia.org/enwiktionary/20260801"
SIMPLE_BASE = "https://dumps.wikimedia.org/simplewiki/20260801"

# (filename, expected bytes, publisher sha1) per dumpstatus.json
# articlesdump job, read 2026-08-17.
WIKT_PARTS: tuple[tuple[str, int, str], ...] = (
    (
        "enwiktionary-20260801-pages-articles1.xml-p1p1500000.bz2",
        321_466_640,
        "393cd735e7600b6f7aeaf54e2a26c7a7a472bb78",
    ),
    (
        "enwiktionary-20260801-pages-articles1.xml-p1500001p3000000.bz2",
        153_867_339,
        "591c68d408c216edfc19d654e7fc73bb8c931837",
    ),
    (
        "enwiktionary-20260801-pages-articles1.xml-p3000001p4500000.bz2",
        169_486_940,
        "623d84231419873addad9f62adfa76adfcbe9a59",
    ),
    (
        "enwiktionary-20260801-pages-articles1.xml-p4500001p6000000.bz2",
        163_289_156,
        "84ea89ab3cfb6bcedb6965bf5dc3b6ab299759e7",
    ),
    (
        "enwiktionary-20260801-pages-articles1.xml-p6000001p7500000.bz2",
        188_466_390,
        "a86352db0e22f12a30843cf444bf1e65a1f253da",
    ),
    (
        "enwiktionary-20260801-pages-articles1.xml-p7500001p9000000.bz2",
        197_154_713,
        "e963edda9f53409da11c91dc6bd8864092cd43dc",
    ),
    (
        "enwiktionary-20260801-pages-articles1.xml-p9000001p10500000.bz2",
        272_861_955,
        "7bd92b4a74fed874ea79468e1b60734645705c28",
    ),
    (
        "enwiktionary-20260801-pages-articles1.xml-p10500001p11899399.bz2",
        156_120_416,
        "aa919a914063d08df0f68018b599c0eaceb92ad6",
    ),
)

SIMPLE_FILE: tuple[str, int, str] = (
    "simplewiki-20260801-pages-articles-multistream.xml.bz2",
    384_058_867,
    "4f646363bd2d095652149a6bdd87a0cedf51f6b2",
)


def _digests(path: Path) -> tuple[str, str]:
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest()


def _fetch_verified(
    url: str, dest: Path, expected_bytes: int, expected_sha1: str
) -> str | None:
    """Download (if needed), verify, return the sha256 pin or None."""
    if dest.exists() and _digests(dest)[0] != expected_sha1:
        print(f"{dest.name}: on disk but sha1 mismatch, re-fetching")
        dest.unlink()
    if not dest.exists():
        print(f"fetching {dest.name} ({expected_bytes:,} bytes) ...")
        subprocess.run(
            ["curl", "-sSL", "--retry", "3", "-o", str(dest), url],
            check=True,
        )
    sha1, sha256 = _digests(dest)
    if sha1 != expected_sha1 or dest.stat().st_size != expected_bytes:
        print(
            f"{dest.name}: FAILED verification (sha1 {sha1} vs "
            f"{expected_sha1}) — deleted, not registered",
            file=sys.stderr,
        )
        dest.unlink(missing_ok=True)
        return None
    print(f"{dest.name}: verified (sha256 {sha256[:16]}...)")
    return sha256


def _upsert(entries: list[dict[str, object]], entry: dict[str, object]) -> None:
    entries[:] = [e for e in entries if e.get("name") != entry["name"]]
    entries.append(entry)


GUARD_NOTE = (
    "Every file verified against the dump's published sha1 at fetch "
    "time; the sha256 per item is over the exact fetched bytes and is "
    "the register's authoritative pin. Fetched under the owner's "
    "broadened go-ahead of 2026-08-17 (register-matched bridge sources "
    "for the W3 successor units), announced with names and sizes "
    "before the fetch; no unit reads these bytes until its declaration "
    "admits them."
)


def main() -> int:
    register = json.loads(REGISTER.read_text())
    entries = register["corpora"]
    today = time.strftime("%Y-%m-%d")
    failures = 0

    wikt_dir = REPO / "bench" / "w" / "corpus" / "enwiktionary-20260801"
    wikt_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, object]] = []
    wikt_total = 0
    for filename, expected_bytes, expected_sha1 in WIKT_PARTS:
        url = f"{WIKT_BASE}/{filename}"
        sha256 = _fetch_verified(
            url, wikt_dir / filename, expected_bytes, expected_sha1
        )
        if sha256 is None:
            failures += 1
            continue
        wikt_total += expected_bytes
        items.append(
            {
                "file": filename,
                "bytes": expected_bytes,
                "publisher_sha1": expected_sha1,
                "sha256": sha256,
                "url": url,
            }
        )
    if len(items) == len(WIKT_PARTS):
        _upsert(
            entries,
            {
                "name": "enwiktionary-20260801-pages-articles",
                "admitted": True,
                "source_url": f"{WIKT_BASE}/",
                "retrieved": today,
                "bytes": wikt_total,
                "items_count": len(items),
                "license": (
                    "CC BY-SA 4.0 (Wiktionary content, per Wikimedia's "
                    "published licensing; dual-licensed GFDL)"
                ),
                "local_path": "bench/w/corpus/enwiktionary-20260801/",
                "verification": {"note": GUARD_NOTE},
                "items": items,
            },
        )

    simple_dir = REPO / "bench" / "w" / "corpus" / "simplewiki-20260801"
    simple_dir.mkdir(parents=True, exist_ok=True)
    filename, expected_bytes, expected_sha1 = SIMPLE_FILE
    url = f"{SIMPLE_BASE}/{filename}"
    sha256 = _fetch_verified(url, simple_dir / filename, expected_bytes, expected_sha1)
    if sha256 is None:
        failures += 1
    else:
        _upsert(
            entries,
            {
                "name": "simplewiki-20260801-pages-articles-multistream",
                "admitted": True,
                "source_url": url,
                "retrieved": today,
                "bytes": expected_bytes,
                "sha256": sha256,
                "publisher_sha1": expected_sha1,
                "license": (
                    "CC BY-SA 4.0 (Simple English Wikipedia article "
                    "text, per Wikimedia's published licensing; "
                    "dual-licensed GFDL)"
                ),
                "local_path": f"bench/w/corpus/simplewiki-20260801/{filename}",
                "verification": {"note": GUARD_NOTE},
            },
        )

    REGISTER.write_text(json.dumps(register, indent=1) + "\n")
    print(f"register updated: {REGISTER.relative_to(REPO)}")
    if failures:
        print(f"{failures} file(s) failed verification", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
