"""Minimal YAML-frontmatter parser, vendored to drop python-frontmatter.

`python-frontmatter` (1.1.0, the current release as of writing) calls
`codecs.open()` in `frontmatter.load()`, which Python 3.14 emits a
`DeprecationWarning` for. The library hasn't shipped a fix, and we only ever
used four entry points (`load`, `loads`, `dumps`, `Post`) — reimplementing
them is shorter than the docstring explaining why we vendor.

Bonus: we avoid the upstream foot-gun where `frontmatter.load(path,
Loader=yaml.SafeLoader)` silently swallows the `Loader` kwarg into
"default metadata" instead of using it. Here we always use pure-Python
`yaml.SafeLoader` / `yaml.SafeDumper` (see store.py for why C-extension
yaml is avoided).

On-disk format (matches what python-frontmatter produced, so existing
memory files keep loading):

    ---
    key: value
    list:
    - item
    ---

    body text
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ._fsutil import bounded_read


_DELIM = "---"

# YAML SafeLoader doesn't protect against alias-expansion DoS — the
# classic "billion laughs" pattern where deeply-nested aliases expand
# to a memory-pinning blob during parse. Memory frontmatter is dozens
# to a few hundred bytes per record in normal use; capping the YAML
# region at 64 KB neutralises the DoS without rejecting any real
# memory file. The store widens its trust boundary once `sync pull`
# is in use (a remote can push files into the memory directory), so
# the cap is no longer purely a defence against local mistakes.
_MAX_YAML_BYTES = 64 * 1024

# File-level cap on `_frontmatter.load`. The previous `read_text()`
# loaded the entire file before the 64 KB YAML cap could fire, so a
# hostile `sync pull` from a remote pushing a multi-GB `.md` exhausted
# memory before the parse path even ran. 1 MiB is ~250× the largest
# legitimate body the project has produced (verified_paths + body
# dense memories cap out under a couple of KB) and 16× the YAML cap,
# so any in-the-wild memory fits comfortably while pathological
# inputs hit a clean ValueError instead of an OOM.
_MAX_FILE_BYTES = 1024 * 1024


@dataclass
class Post:
    """A parsed frontmatter document. Mirror of `frontmatter.Post`."""

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def loads(text: str) -> Post:
    """Parse a frontmatter string into a Post.

    If `text` doesn't begin with a `---` line, the whole thing is body and
    metadata is empty — same as python-frontmatter.
    """
    # Split into lines but remember whether the input had a trailing newline,
    # so the body round-trips. Strip CR so CRLF line endings work.
    raw_lines = text.split("\n")
    lines = [ln.rstrip("\r") for ln in raw_lines]

    # Opening delimiter must be `---` on its own line, line 0.
    if not lines or lines[0] != _DELIM:
        return Post(content=text, metadata={})

    # Find the closing `---` on its own line, somewhere after line 0.
    close_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i] == _DELIM:
            close_idx = i
            break
    if close_idx is None:
        # No closing delimiter — treat the whole text as body.
        return Post(content=text, metadata={})

    yaml_text = "\n".join(lines[1:close_idx])
    if len(yaml_text.encode("utf-8")) > _MAX_YAML_BYTES:
        # Pre-flight size check. Reject before yaml.load gets a chance
        # to start expanding aliases. Real memories don't approach this
        # ceiling — the largest legitimate frontmatter the project
        # produces (a memory with dense `verified_paths` /
        # `verified_commits` and several `links`) is a couple of KB.
        raise ValueError(
            f"frontmatter YAML exceeds {_MAX_YAML_BYTES}-byte cap "
            f"({len(yaml_text)} chars); refusing to parse"
        )
    body_lines = lines[close_idx + 1 :]
    # Drop a single separator blank line, mirroring python-frontmatter's
    # `---\n\n<body>` shape.
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)

    # Translate the yaml package's exception hierarchy into ValueError
    # at the parser boundary. `yaml.YAMLError` inherits from `Exception`,
    # NOT from `ValueError`, so downstream callers in `store.py` that
    # catch `(ValueError, KeyError, OSError)` to skip malformed files
    # would otherwise crash on a single corrupt file — torn writes from
    # `sync pull`, hand-edit typos, partial-write recovery. The
    # frontmatter module owns the YAML boundary, so the translation
    # belongs here, not threaded through every callsite. `from exc`
    # preserves the original `yaml.YAMLError` on `__cause__` for
    # debugging.
    try:
        metadata = yaml.load(yaml_text, Loader=yaml.SafeLoader) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed YAML: {exc}") from exc
    if not isinstance(metadata, dict):
        # Frontmatter must be a mapping; anything else is malformed.
        raise ValueError("frontmatter metadata must be a YAML mapping")

    return Post(content=body, metadata=metadata)


def load(path: Path | str) -> Post:
    """Read and parse a file. Replaces `frontmatter.load(path, ...)`.

    Stat-rejects files larger than :data:`_MAX_FILE_BYTES` before any
    allocation — the previous unbounded ``read_text()`` was a sync-pull
    DoS vector (a hostile remote pushing a multi-GB ``.md`` would OOM
    the loader before the 64 KB YAML cap ever fired).

    Decodes UTF-8 strictly: invalid bytes raise ``ValueError``. Not
    ``errors="replace"`` — substituting U+FFFD would let a corrupt
    memory file load into the retrieval surface with `doctor`
    reporting it clean, and the next mutator would rewrite the file,
    laundering the corruption permanently. Raising keeps the
    pre-2.6.4 contract (``read_text(encoding="utf-8")`` raised here
    too) so the store's malformed-file skip path fires.

    Re-raises `ValueError` from :func:`loads` with the file path
    prefixed — `loads` doesn't know which path it came from, but
    diagnostic output (the store's `doctor`, error logs) is dramatically
    more useful when the path is named.
    """
    raw = bounded_read(Path(path), _MAX_FILE_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8: {exc}") from exc
    try:
        return loads(text)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


class _NoAliasDumper(yaml.SafeDumper):
    """SafeDumper variant that never emits YAML anchors/aliases.

    Without this, pyyaml emits `&idN` / `*idN` syntax whenever two metadata
    fields reference the same object — e.g. `created` / `updated` on a fresh
    write, where both come from the same `utcnow()` call. The output is
    valid YAML and round-trips, but humans grep memory files and the alias
    form is harder to read than two literal timestamps.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True


def dumps(post: Post) -> str:
    """Serialise a Post to a frontmatter string.

    Matches python-frontmatter's output: `---\\n<yaml>\\n---\\n\\n<body>`,
    with `body` right-stripped of trailing whitespace (leading whitespace
    preserved). Existing files written by the previous library round-trip
    byte-for-byte (modulo the alias-suppression policy above, which only
    affects metadata dicts where two fields share the same object).
    """
    yaml_text = yaml.dump(
        post.metadata,
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    # Write-side mirror of the `loads` read cap. Without this, a write whose
    # serialized frontmatter exceeds `_MAX_YAML_BYTES` (e.g. many/long
    # `links` notes or `verified_paths`/`verified_commits` entries) succeeds
    # on disk but then fails to PARSE on every subsequent read — the store's
    # malformed-file skip silently drops the record from search/list/show/
    # health while the write reported committed. Catching it here, at the one
    # chokepoint every persist routes through, turns that silent permanent
    # data loss into a clean ValueError (surfaced as a structured tool error)
    # for ALL frontmatter fields, present and future — not field-by-field.
    yaml_bytes = len(yaml_text.encode("utf-8"))
    if yaml_bytes > _MAX_YAML_BYTES:
        raise ValueError(
            f"frontmatter YAML exceeds {_MAX_YAML_BYTES}-byte cap "
            f"({yaml_bytes} bytes); refusing to write — a file this large "
            "would be rejected on read, silently dropping the record"
        )
    body = post.content.rstrip()
    return f"{_DELIM}\n{yaml_text}\n{_DELIM}\n\n{body}"


__all__ = ["Post", "load", "loads", "dumps"]
