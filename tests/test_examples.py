"""Schema-rot guard for shipped example memories (M8 audit).

`examples/memories/*.md` ships in the repo as a curated "here's what a
real bettermemory store looks like" reference. The files double as
onboarding material (README points at them) and as a smoke-test corpus
for anyone hand-trying the parser.

The fixtures are mostly inert — schema bumps tend to touch the
production reader without touching this directory, so a future
additive-field migration that *requires* the new field (rather than
defaulting it on missing) would silently rot the examples without
anyone noticing until a user filed "your example doesn't parse" months
later.

This test walks every shipped example, parses it via the same
frontmatter loader the store uses, and validates the result against
`Memory.model_validate`. If a schema bump rots the examples, the
test fails immediately and the contributor knows to update the
fixtures alongside the schema change.

We intentionally re-implement the schema-version + additive-field
plumbing from `Store._load_path` (rather than calling it directly)
because:

1. `Store._load_path` is a private method; depending on it from a
   test pins the internal name.
2. The examples may legitimately omit additive fields (`origin`,
   `category`, `links`, `verified_*`) — testing the public
   `Memory.model_validate` round-trip is the right contract surface.
3. The store wraps `load_one` / `load_all` with extra concerns
   (tombstones, schema-version gating) that are noise for an
   example-validation test.
"""

from __future__ import annotations

import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import _frontmatter as frontmatter
from bettermemory.models import (
    Category,
    Confidence,
    Memory,
    MemoryLink,
    SCHEMA_VERSION,
    Source,
)
from bettermemory.origin import Origin


_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLES_DIR = _REPO_ROOT / "examples" / "memories"


def _example_memory_files() -> list[Path]:
    """All shipped example `.md` files, excluding `README.md`."""
    if not _EXAMPLES_DIR.is_dir():
        return []
    return sorted(
        p
        for p in _EXAMPLES_DIR.glob("*.md")
        if p.name != "README.md" and not p.name.startswith(".")
    )


def _coerce_dt(value: object) -> datetime:
    """Best-effort coercion of a frontmatter timestamp to an aware datetime.

    PyYAML emits a `datetime` for unquoted ISO timestamps; quoted
    strings come back as `str`. Both shapes occur in the example
    files in the wild, so handle both.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    raise TypeError(f"unexpected datetime shape: {type(value).__name__}")


def test_examples_directory_is_not_empty() -> None:
    """Sanity check that the example directory actually has files —
    a future restructure could move them and this test would otherwise
    silently pass with zero coverage."""
    files = _example_memory_files()
    assert files, (
        f"no example memory files found in {_EXAMPLES_DIR}. "
        f"Either the README link is dead or the test path is stale."
    )


@pytest.mark.parametrize(
    "example_path",
    _example_memory_files(),
    ids=lambda p: p.name,
)
def test_example_memory_parses_and_validates(example_path: Path) -> None:
    """Every shipped example must parse cleanly and round-trip through
    `Memory.model_validate`. Catches schema rot at PR time rather than
    at "a user filed an issue" time."""
    post = frontmatter.load(example_path)
    meta = post.metadata
    assert isinstance(meta, dict), (
        f"{example_path.name}: frontmatter must be a YAML mapping, "
        f"got {type(meta).__name__}"
    )

    # Schema version — the on-disk format predates the field, so
    # missing = 1 (legacy implicit). Anything newer than this reader
    # supports is a contributor mistake and should fail loudly.
    on_disk_version = meta.get("schema_version", 1)
    assert int(on_disk_version) <= SCHEMA_VERSION, (
        f"{example_path.name}: schema_version {on_disk_version} > "
        f"reader's max {SCHEMA_VERSION}. Update the reader or the file."
    )

    # Required fields per Memory model.
    required = ("id", "created", "updated", "scopes", "confidence", "source")
    missing = [k for k in required if k not in meta]
    assert not missing, f"{example_path.name}: missing required field(s): {missing}"

    # Build the kwargs dict the way Store._load_path does, with the
    # additive-field defaults that legacy memories rely on. Anything
    # the example file omits gets the documented default.
    origin_raw = meta.get("origin")
    origin = Origin.model_validate(origin_raw) if isinstance(origin_raw, dict) else None

    category_raw = meta.get("category")
    if category_raw is None:
        category: Category | None = None
    else:
        category = Category(str(category_raw))

    last_verified_raw = meta.get("last_verified_at")
    last_verified_at: datetime | None
    if last_verified_raw is None:
        last_verified_at = None
    else:
        last_verified_at = _coerce_dt(last_verified_raw)

    links_raw = meta.get("links")
    links: list[MemoryLink] = []
    if isinstance(links_raw, list):
        for entry in links_raw:
            if not isinstance(entry, dict):
                continue
            links.append(MemoryLink.model_validate(entry))

    def _str_list(value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError(
                f"{example_path.name}: expected list, got {type(value).__name__}"
            )
        return [str(item) for item in value]

    memory = Memory.model_validate(
        {
            "id": str(meta["id"]),
            "created": _coerce_dt(meta["created"]),
            "updated": _coerce_dt(meta["updated"]),
            "scopes": list(meta["scopes"]),
            "confidence": Confidence(meta["confidence"]),
            "source": Source(meta["source"]),
            "body": post.content.strip() + "\n",
            "origin": origin,
            "last_verified_at": last_verified_at,
            "category": category,
            "verified_paths": _str_list(meta.get("verified_paths")),
            "verified_commits": _str_list(meta.get("verified_commits")),
            "verified_versions": _str_list(meta.get("verified_versions")),
            "links": links,
        }
    )
    # Round-trip sanity — id and body survive validation.
    assert memory.id == str(meta["id"])
    assert memory.body.strip(), f"{example_path.name}: body is empty after validation"


def test_mypy_numpy_override_skips_follow_imports() -> None:
    """mypy silently type-checks NOTHING when the `embeddings` extra is
    installed unless numpy's imports are skipped.

    With numpy present, `python_version = "3.11"`, mypy follows the
    numpy import and tries to parse numpy 2.x's `__init__.pyi`, which
    uses the 3.12-only `type` statement. That aborts the whole run
    ('errors prevented further checking') and the bettermemory package
    goes unchecked. The guard: numpy must have a dedicated override with
    `follow_imports = "skip"` so mypy never opens numpy's stubs.

    python_version MUST stay pinned to the project minimum (3.11) so
    3.11-incompatible syntax in our own code keeps getting flagged —
    assert that here too, so a future 'just bump it to 3.12' shortcut
    (which would mask this by making numpy's stubs parse) trips a red
    test instead of silently weakening the type gate.
    """
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    mypy = cfg["tool"]["mypy"]
    assert mypy["python_version"] == "3.11", (
        "mypy python_version must stay at the project minimum (3.11); "
        "bumping it to 3.12 silently lets 3.12-only syntax through and "
        "also masks the numpy-stub parse abort this test guards."
    )

    overrides = mypy.get("overrides", [])

    def _modules(block: dict[str, object]) -> list[str]:
        mod = block.get("module", [])
        if isinstance(mod, str):
            return [mod]
        if isinstance(mod, list):
            return [str(m) for m in mod]
        return []

    numpy_blocks = [
        block
        for block in overrides
        if any(m == "numpy" or m == "numpy.*" for m in _modules(block))
    ]
    assert numpy_blocks, (
        "expected a [[tool.mypy.overrides]] block matching numpy — "
        "without one, an installed numpy's 3.12-syntax stubs abort the "
        "whole mypy run and the package goes unchecked."
    )
    # Both settings are load-bearing and must live on the SAME override block:
    # `follow_imports = "skip"` is IGNORED for stub (.pyi) files by default, and
    # numpy ships its types as bundled .pyi — so without
    # `follow_imports_for_stubs = true` mypy STILL opens numpy's __init__.pyi
    # and aborts on its 3.12-only `type` statement (verified: skip alone leaves
    # mypy checking 0 files). Assert both on one block so a future edit can't
    # drop the stub setting and silently reintroduce the no-op.
    assert any(
        block.get("follow_imports") == "skip"
        and block.get("follow_imports_for_stubs") is True
        for block in numpy_blocks
    ), (
        "the numpy mypy override must set BOTH follow_imports = 'skip' AND "
        "follow_imports_for_stubs = true. ignore_missing_imports alone does "
        "NOT stop mypy from parsing an *installed* numpy's __init__.pyi, and "
        "follow_imports='skip' alone is ignored for numpy's bundled .pyi "
        "stubs — so mypy parses them and its 3.12-only `type` statement "
        "aborts type-checking under python_version 3.11."
    )
