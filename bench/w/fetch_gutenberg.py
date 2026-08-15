"""Fetch the W1 register's curated Project Gutenberg subset, verified.

The fetch step `bench/w/W1_DECLARATION.md` §3 names, run once under the
owner's plain-sentence yes of 2026-08-15. The list below is the
hand-curated selection the declaration's constraint ledger requires:
canonical English-language works chosen by title, no popularity feed,
no generated list. Every candidate is admitted to the register only if
ALL of the following hold, checked mechanically against the fetched
bytes:

- the Project Gutenberg header carries the standard no-cost /
  almost-no-restrictions license sentence (the public-domain form; a
  copyright-noticed eBook fails this line and is recorded, not kept);
- the header's ``Language:`` line says English;
- the header's ``Title:`` line matches the curated expectation, so a
  renumbered or reassigned ID cannot silently swap a different book in;
- per-file and whole-set size caps hold (the declaration caps the set
  at 50 MB).

Rejections are recorded beside admissions in the manifest so the
register can carry ``admitted=false`` entries with their reason — the
program register's own rule. The manifest
(``bench/w/corpus/gutenberg/manifest.json``) records, per admitted
item: id, expected and actual title, URL, byte count, sha256 over the
exact fetched bytes, and the matched license line. The corpus bytes
stay out of git (``bench/w/corpus/`` is ignored); the register commits
the pins.

Politeness: one request at a time, a fixed delay between requests, an
identifying User-Agent, and the plain-text cache endpoint only.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

# The standard header sentence carried by Project Gutenberg's
# public-domain texts. Copyright-noticed eBooks carry a different
# sentence and are refused by its absence.
_LICENSE_MARK = "anyone anywhere in the United States"

_UA = "bettermemory-bench-W1/1.0 (one-time corpus pin; see bench/w/W1_DECLARATION.md)"
_DELAY_SECONDS = 1.5
_PER_FILE_CAP = 6_000_000
_SET_CAP = 50_000_000

# id -> title substring expected on the fetched header's Title: line
# (case-insensitive). Hand-curated: fiction and nonfiction breadth,
# every entry a canonical public-domain English-language text.
CURATED: dict[int, str] = {
    1342: "Pride and Prejudice",
    158: "Emma",
    161: "Sense and Sensibility",
    105: "Persuasion",
    121: "Northanger Abbey",
    141: "Mansfield Park",
    84: "Frankenstein",
    345: "Dracula",
    11: "Alice's Adventures in Wonderland",
    12: "Through the Looking-Glass",
    1661: "The Adventures of Sherlock Holmes",
    2852: "The Hound of the Baskervilles",
    244: "A Study in Scarlet",
    2097: "The Sign of the Four",
    834: "Memoirs of Sherlock Holmes",
    108: "The Return of Sherlock Holmes",
    3289: "The Valley of Fear",
    2701: "Moby Dick",
    98: "A Tale of Two Cities",
    1400: "Great Expectations",
    766: "David Copperfield",
    730: "Oliver Twist",
    1023: "Bleak House",
    786: "Hard Times",
    46: "A Christmas Carol",
    580: "The Pickwick Papers",
    74: "The Adventures of Tom Sawyer",
    76: "Huckleberry Finn",
    86: "A Connecticut Yankee in King Arthur's Court",
    3176: "The Innocents Abroad",
    245: "Life on the Mississippi",
    1080: "A Modest Proposal",
    829: "Gulliver's Travels",
    174: "The Picture of Dorian Gray",
    844: "The Importance of Being Earnest",
    43: "Jekyll",
    120: "Treasure Island",
    1260: "Jane Eyre",
    768: "Wuthering Heights",
    969: "The Tenant of Wildfell Hall",
    145: "Middlemarch",
    550: "Silas Marner",
    2554: "Crime and Punishment",
    28054: "The Brothers Karamazov",
    600: "Notes from the Underground",
    2600: "War and Peace",
    1399: "Anna Karenina",
    1184: "The Count of Monte Cristo",
    1257: "The Three Musketeers",
    135: "Les Mis",
    2610: "Notre-Dame",
    36: "The War of the Worlds",
    35: "The Time Machine",
    5230: "The Invisible Man",
    159: "The Island of Doctor Moreau",
    164: "Twenty Thousand Leagues",
    103: "Around the World in Eighty Days",
    18857: "Journey to the Interior of the Earth",
    521: "Robinson Crusoe",
    2591: "Grimms' Fairy Tales",
    1952: "The Yellow Wallpaper",
    33: "The Scarlet Letter",
    77: "The House of the Seven Gables",
    2147: "Edgar Allan Poe",
    2148: "Edgar Allan Poe",
    205: "Walden",
    1322: "Leaves of Grass",
    219: "Heart of Darkness",
    974: "Lord Jim",
    113: "The Secret Garden",
    514: "Little Women",
    271: "Black Beauty",
    236: "The Jungle Book",
    2226: "Kim",
    910: "White Fang",
    215: "The Call of the Wild",
    64317: "The Great Gatsby",
    4300: "Ulysses",
    2814: "Dubliners",
    4217: "A Portrait of the Artist as a Young Man",
    100: "Shakespeare",
    996: "Don Quixote",
    1727: "The Odyssey",
    6130: "The Iliad",
    1497: "The Republic",
    3207: "Leviathan",
    3600: "Essays of Michel de Montaigne",
    2680: "Meditations",
    1232: "The Prince",
    132: "The Art of War",
    16328: "Beowulf",
    408: "The Souls of Black Folk",
    203: "Uncle Tom's Cabin",
    23: "Frederick Douglass",
    1404: "The Federalist Papers",
    1228: "On the Origin of Species",
    944: "The Voyage of the Beagle",
    4363: "Beyond Good and Evil",
    1998: "Thus Spake Zarathustra",
    5827: "The Problems of Philosophy",
    61: "The Communist Manifesto",
    3300: "An Inquiry into the Nature and Causes of the Wealth of Nations",
    34901: "On Liberty",
    55: "The Wonderful Wizard of Oz",
    16: "Peter Pan",
    289: "The Wind in the Willows",
    45: "Anne of Green Gables",
    41: "The Legend of Sleepy Hollow",
    62: "A Princess of Mars",
    78: "Tarzan of the Apes",
    209: "The Turn of the Screw",
    541: "The Age of Innocence",
    284: "The House of Mirth",
    140: "The Jungle",
    155: "The Moonstone",
    583: "The Woman in White",
    863: "The Mysterious Affair at Styles",
    1155: "The Secret Adversary",
    696: "The Castle of Otranto",
    8800: "Divine Comedy",
    26: "Paradise Lost",
    2383: "Canterbury Tales",
    5000: "Leonardo da Vinci",
    8492: "The King in Yellow",
}


def _header(text: str) -> str:
    """The Project Gutenberg header: everything before the START marker."""
    match = re.search(r"\*\*\* ?START OF", text)
    return text[: match.start()] if match else text[:4000]


def _header_field(header: str, name: str) -> str:
    match = re.search(rf"^{name}:\s*(.+)$", header, re.I | re.M)
    return match.group(1).strip() if match else ""


def fetch_one(book_id: int) -> tuple[bytes, str]:
    url = f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt"
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read(), url


def main() -> int:
    out_dir = Path(__file__).parent / "corpus" / "gutenberg"
    out_dir.mkdir(parents=True, exist_ok=True)
    admitted: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    total = 0

    for book_id, expected_title in sorted(CURATED.items()):
        time.sleep(_DELAY_SECONDS)
        try:
            data, url = fetch_one(book_id)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            rejected.append(
                {
                    "id": book_id,
                    "expected_title": expected_title,
                    "reason": f"fetch failed: {exc}",
                }
            )
            print(f"REJECT {book_id} ({expected_title}): fetch failed: {exc}")
            continue

        text = data.decode("utf-8", errors="replace")
        header = _header(text)
        title = _header_field(header, "Title")
        language = _header_field(header, "Language")
        license_line = next(
            (ln.strip() for ln in header.splitlines() if _LICENSE_MARK in ln), ""
        )

        reason = ""
        if not license_line:
            reason = "no public-domain license sentence in header"
        elif "english" not in language.lower():
            reason = f"language is {language!r}, not English"
        elif expected_title.lower() not in title.lower():
            reason = f"title mismatch: header says {title!r}"
        elif len(data) > _PER_FILE_CAP:
            reason = f"file exceeds per-file cap ({len(data)} bytes)"
        elif total + len(data) > _SET_CAP:
            reason = "set cap would be exceeded"

        if reason:
            rejected.append(
                {"id": book_id, "expected_title": expected_title, "reason": reason}
            )
            print(f"REJECT {book_id} ({expected_title}): {reason}")
            continue

        (out_dir / f"pg{book_id}.txt").write_bytes(data)
        total += len(data)
        admitted.append(
            {
                "id": book_id,
                "title": title,
                "expected_title": expected_title,
                "url": url,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "license_line": license_line,
                "language": language,
            }
        )
        print(f"ok {book_id} {title} ({len(data)} bytes)")

    manifest = {
        "retrieved": "2026-08-15",
        "endpoint": "https://www.gutenberg.org/cache/epub/<id>/pg<id>.txt",
        "license_check": _LICENSE_MARK,
        "total_bytes": total,
        "admitted": admitted,
        "rejected": rejected,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"\nadmitted {len(admitted)}, rejected {len(rejected)}, total {total} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
