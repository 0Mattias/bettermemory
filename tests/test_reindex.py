"""Tests for `bettermemory reindex --embeddings` (the cache-warming path).

The behavioural contract under test: warming the persistent embedding
cache must embed the SAME text the readers (search / dedup / consolidate)
embed — `memory.body.strip()` — under the same `(memory_id, freshness)`
cache key. The cache is keyed on `(id, updated)`, NOT on the body text,
so warming from the unstripped body would write a wrong-text vector under
the readers' own key; a later strip-then-lookup would then read back a
vector computed on different text.

The semantic plumbing (`cached_embed`, the model loader, the cache flush)
is patched at its fully-qualified module path so these tests never touch a
real embedding model or the on-disk cache — they assert only on which
text reaches `cached_embed`.
"""

from __future__ import annotations
