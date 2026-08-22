"""Fetch and pin BEIR-format retrieval datasets.

Category instruments for encoder reads: standard public BEIR datasets
(corpus.jsonl / queries.jsonl / qrels/test.tsv inside a zip), fetched
from the canonical hosting and pinned by sha256 in ``PINS.json``
beside this script. The zip bytes are the pin; extraction is
mechanical. Data lands under ``data/`` (gitignored — the pin file is
the committed artifact, the bytes are reproducible from it).

A first fetch of a dataset records its entry with ``--pin``; every
later fetch verifies against the recorded sha and refuses a mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

_HERE = Path(__file__).parent
PINS_PATH = _HERE / "PINS.json"
DATA_DIR = _HERE / "data"
BASE_URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(name: str, pin: bool) -> int:
    pins = json.loads(PINS_PATH.read_text()) if PINS_PATH.exists() else {}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DATA_DIR / f"{name}.zip"
    url = f"{BASE_URL}/{name}.zip"

    if not zip_path.exists():
        print(f"fetching {url}", flush=True)
        with urllib.request.urlopen(url) as resp, zip_path.open("wb") as out:
            while block := resp.read(1 << 20):
                out.write(block)
    sha = _sha256(zip_path)

    if name in pins:
        if pins[name]["zip_sha256"] != sha:
            print(
                f"PIN MISMATCH for {name}: recorded {pins[name]['zip_sha256']}, "
                f"fetched {sha}",
                file=sys.stderr,
            )
            return 1
        print(f"{name}: pin verified ({sha[:16]}…)")
    elif pin:
        pins[name] = {
            "url": url,
            "zip_sha256": sha,
            "bytes": zip_path.stat().st_size,
        }
        PINS_PATH.write_text(json.dumps(pins, indent=1, sort_keys=True) + "\n")
        print(f"{name}: pinned {sha}")
    else:
        print(f"{name}: UNPINNED ({sha}); re-run with --pin to record", file=sys.stderr)
        return 1

    extract_dir = DATA_DIR / name
    if not extract_dir.exists():
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(DATA_DIR)
        print(f"extracted to {extract_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="+", help="BEIR dataset names")
    parser.add_argument("--pin", action="store_true", help="record first-fetch pins")
    args = parser.parse_args()
    for name in args.names:
        code = fetch(name, args.pin)
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
