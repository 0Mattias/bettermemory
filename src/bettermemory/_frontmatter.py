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


_DELIM = "---"


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
    body_lines = lines[close_idx + 1 :]
    # Drop a single separator blank line, mirroring python-frontmatter's
    # `---\n\n<body>` shape.
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)

    metadata = yaml.load(yaml_text, Loader=yaml.SafeLoader) or {}
    if not isinstance(metadata, dict):
        # Frontmatter must be a mapping; anything else is malformed.
        raise ValueError("frontmatter metadata must be a YAML mapping")

    return Post(content=body, metadata=metadata)


def load(path: Path | str) -> Post:
    """Read and parse a file. Replaces `frontmatter.load(path, ...)`."""
    return loads(Path(path).read_text(encoding="utf-8"))


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
    body = post.content.rstrip()
    return f"{_DELIM}\n{yaml_text}\n{_DELIM}\n\n{body}"


__all__ = ["Post", "load", "loads", "dumps"]
