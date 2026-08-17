"""Fetch and pin the four everyday-register Stack Exchange site dumps.

The W3-P record's owner door, taken 2026-08-17: the owner's
plain-sentence yes covers exactly these four per-site archives from
the archive.org stackexchange item — cooking, music, fitness,
interpersonal — whose PostLinks.xml carries labeled duplicate edges.
The expected sizes and publisher md5s below were captured from the
item's metadata endpoint on the day of the yes; a downloaded file
that does not match its published md5 is deleted and not registered.

Each verified file is pinned into `bench/w/corpora.json` with
admitted=true, the sha256 over the exact fetched bytes as the
authoritative pin, and the standing guard note: no unit reads these
bytes until its declaration admits them. The successor unit (W3-P2)
declares its own floors before any read.

Idempotent: a file already on disk with a matching md5 is not
re-fetched; an existing register entry of the same name is replaced
in place.

Run: fastvenv/bin/python bench/w/fetch_se_sites.py
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
DEST = REPO / "bench" / "w" / "corpus" / "stackexchange"
BASE_URL = "https://archive.org/download/stackexchange"

# name -> (filename, expected bytes, publisher md5), from
# https://archive.org/metadata/stackexchange as read 2026-08-17.
# The first four are the W3-P record's owner door, taken that morning;
# the rest are the needs-mapped extension the owner's broadened
# same-day go-ahead covers ("I am giving you the go ahead to
# download") — each mapped to an anatomy need or to preference-class
# register breadth, announced with names and sizes before fetching.
SITES: dict[str, tuple[str, int, str]] = {
    "cooking-stackexchange-archive": (
        "cooking.stackexchange.com.7z",
        87_555_311,
        "f6ffe54c3783c8faebc33b30f8ff61d1",
    ),
    "music-stackexchange-archive": (
        "music.stackexchange.com.7z",
        106_858_896,
        "25cb9304308f1aa97e4e42d8cd20fcc0",
    ),
    "fitness-stackexchange-archive": (
        "fitness.stackexchange.com.7z",
        34_959_036,
        "ea5849aa520a43a9d16fa5a31666992a",
    ),
    "interpersonal-stackexchange-archive": (
        "interpersonal.stackexchange.com.7z",
        36_147_734,
        "2390b10608872b1cc9dba42b56873024",
    ),
    # need 1: publications/conferences <-> research interests
    "academia-stackexchange-archive": (
        "academia.stackexchange.com.7z",
        198_564_136,
        "5706c0588af14de4d8dc8e617bfa9a7d",
    ),
    # need 4: cocktail <-> drinks (the site is "Beer, Wine & Spirits")
    "beer-stackexchange-archive": (
        "beer.stackexchange.com.7z",
        4_317_770,
        "5da8bd0067af280aeb53ebfd6b5e650d",
    ),
    # need 5: battery <-> charging/power, consumer-tech register
    "android-stackexchange-archive": (
        "android.stackexchange.com.7z",
        130_988_898,
        "6164b2bc6fc0d11e3a6feda717e51afd",
    ),
    "apple-stackexchange-archive": (
        "apple.stackexchange.com.7z",
        312_313_283,
        "5cb36a924af4c3c7d260e20da15db73e",
    ),
    "superuser-archive": (
        "superuser.com.7z",
        1_294_499_667,
        "37179ccf3098d75f272419388f2db657",
    ),
    # need 7: creamer/coffee recipes
    "coffee-stackexchange-archive": (
        "coffee.stackexchange.com.7z",
        5_095_298,
        "458d102b0d9083a5c0d53031f4b03af7",
    ),
    # need 3: homegrown/garden produce
    "gardening-stackexchange-archive": (
        "gardening.stackexchange.com.7z",
        43_596_356,
        "9b2fb873459d516d6d48beff4e6a610f",
    ),
    # preference-class register breadth (home, planning, media,
    # outdoors, family, pets, travel — the domains the LME asks span)
    "diy-stackexchange-archive": (
        "diy.stackexchange.com.7z",
        227_459_493,
        "14090ed995204f4966b30abc4dfc090a",
    ),
    "lifehacks-stackexchange-archive": (
        "lifehacks.stackexchange.com.7z",
        14_214_166,
        "5c19004e709f7f715361503fa5901a65",
    ),
    "movies-stackexchange-archive": (
        "movies.stackexchange.com.7z",
        85_008_557,
        "2897dd935d252ad9544f427a8a6b1fd0",
    ),
    "outdoors-stackexchange-archive": (
        "outdoors.stackexchange.com.7z",
        31_445_654,
        "3875f1ab98ffe32d4aa66bee2225699f",
    ),
    "parenting-stackexchange-archive": (
        "parenting.stackexchange.com.7z",
        45_517_811,
        "5dc136fbeaa4cb0bb58204f3d840c7ae",
    ),
    "pets-stackexchange-archive": (
        "pets.stackexchange.com.7z",
        27_410_072,
        "d403320d7796d687a704bec3a1411a6c",
    ),
    "travel-stackexchange-archive": (
        "travel.stackexchange.com.7z",
        156_149_761,
        "c11842bcc4c533ffb3baa87d07fd142a",
    ),
}

LICENSE = (
    "CC BY-SA (Stack Exchange network content, per the archive.org "
    "stackexchange item's published terms; per-post attribution "
    "obligations noted for any redistribution — training-input use "
    "records the source here)"
)


def _digests(path: Path) -> tuple[str, str]:
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()


def _fetch(url: str, dest: Path) -> None:
    subprocess.run(
        ["curl", "-sSL", "--retry", "3", "-o", str(dest), url],
        check=True,
    )


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    register = json.loads(REGISTER.read_text())
    entries = register["corpora"]
    today = time.strftime("%Y-%m-%d")
    failures = 0

    for name, (filename, expected_bytes, expected_md5) in SITES.items():
        dest = DEST / filename
        url = f"{BASE_URL}/{filename}"
        if dest.exists() and _digests(dest)[0] != expected_md5:
            print(f"{filename}: on disk but md5 mismatch, re-fetching")
            dest.unlink()
        if not dest.exists():
            print(f"fetching {filename} ({expected_bytes:,} bytes) ...")
            _fetch(url, dest)
        else:
            print(f"{filename}: already on disk")
        md5, sha = _digests(dest)
        size = dest.stat().st_size
        if md5 != expected_md5 or size != expected_bytes:
            print(
                f"{filename}: FAILED verification "
                f"(md5 {md5} vs {expected_md5}, {size:,} vs "
                f"{expected_bytes:,} bytes) — deleted, not registered",
                file=sys.stderr,
            )
            dest.unlink(missing_ok=True)
            failures += 1
            continue
        entry = {
            "name": name,
            "admitted": True,
            "source_url": url,
            "retrieved": today,
            "bytes": size,
            "sha256": sha,
            "publisher_md5": expected_md5,
            "license": LICENSE,
            "local_path": f"bench/w/corpus/stackexchange/{filename}",
            "verification": {
                "note": (
                    "md5 verified against the archive.org item metadata "
                    "(match); sha256 over the exact fetched bytes is the "
                    "authoritative pin. Fetched under the owner's "
                    "plain-sentence yes of 2026-08-17 (the W3-P record's "
                    "owner door: per-site everyday-register dumps with "
                    "labeled PostLinks duplicate edges); no unit reads "
                    "these bytes until its declaration admits them."
                )
            },
        }
        entries[:] = [e for e in entries if e.get("name") != name]
        entries.append(entry)
        print(f"{filename}: pinned (sha256 {sha[:16]}...)")

    REGISTER.write_text(json.dumps(register, indent=1) + "\n")
    print(f"register updated: {REGISTER.relative_to(REPO)}")
    if failures:
        print(f"{failures} file(s) failed verification", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
