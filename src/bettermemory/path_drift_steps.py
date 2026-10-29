"""The body-candidate pass of `verify_anchoring.detect_path_drift`.

`_check_body_candidates` existence-checks the path candidates extracted
from a memory's body (`verify_candidates._extract_candidates`) when no
claim anchor is given, and appends each to the read's buckets: `checked`,
`missing` with its claim-anchored subset, `verified`, `expected_absent`
and `dropped_as_route`. A candidate that is missing and unattested is
arbitrated first: a spaced bare-scan run is checked by its prefix, an
ambiguous truncation is dropped, and an application route is recorded as
one, in that order (`tests/test_verify.py` pins the route check last).
Like `_check_anchored_attestations` and `_check_anchored_citations`, the
passes the read runs after this one, it appends to the buckets and the
seen set it is handed.

This pass lives apart from `verify_anchoring.py` to keep it under the
package's 1,900-line watch line (ENG-97). `verify_anchoring` imports this
module and this module never imports it: from the package it imports
`verify_candidates` alone, whose `_path_exists` a test patches through
that module's `Path`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .verify_candidates import _is_multi_segment_routelike, _path_exists


def _check_body_candidates(
    candidates: Sequence[tuple[str, bool, bool]],
    verified_set: set[str],
    absent_set: set[str],
    *,
    checked: list[str],
    missing: list[str],
    claim_anchored: list[str],
    verified: list[str],
    expected_absent: list[str],
    dropped_as_route: list[str],
    seen: set[str],
    keys: Callable[[str], str],
) -> None:
    """Take the body's candidates; sort each into the buckets handed in, return None."""
    for path, drop_if_missing, bare_spaced in candidates:
        exists = _path_exists(path)
        norm = keys(path)
        # An attestation pins the citation: a path the caller explicitly
        # named in `verified_paths` / `verified_absent_paths` is proven to
        # be the complete, intended claim, which resolves any extraction
        # ambiguity — the drops below never apply to attested candidates
        # (otherwise a verified-then-deleted path would produce NO drift
        # signal, breaking the documented contract).
        attested = norm in verified_set or norm in absent_set
        if not exists and not attested:
            if bare_spaced:
                # Existence-arbitrated fallback for spaced bare-scan
                # candidates: the continuation rule can glue a prose
                # acronym pair (`TCP/IP`, `CI/CD`) onto a real path
                # (`/etc/hosts TCP/IP keepalive`). If the prefix up to
                # the first space exists, the spaced run was prose —
                # check the prefix instead, so the real path still gets
                # its drift check. If neither form exists, the extraction
                # is too ambiguous to trust; drop rather than flag a
                # claim we may have manufactured. (Backticked spaced
                # paths never take this branch — the author delimited
                # those precisely, so a miss there is real drift.)
                prefix = path.split(" ", 1)[0]
                prefix_key = keys(prefix)
                if _path_exists(prefix) and prefix_key not in seen:
                    seen.add(prefix_key)
                    checked.append(prefix)
                    if prefix_key in verified_set:
                        verified.append(prefix)
                continue
            if drop_if_missing:
                # Ambiguous truncation: the bare scan stopped at
                # ` Capitalized…` (or ` (2).pdf`-style continuations)
                # right after this candidate, so the real citation may
                # continue past the space. A missing-flag here would be
                # manufactured by our own truncation, not by drift.
                # An existing candidate is kept: existence proves the
                # prefix is a real path regardless of the prose after it.
                continue
            if _is_multi_segment_routelike(path):
                # An application route, not a deleted file. `_is_route`
                # upstream can only suppress routes when the SAME body
                # also happens to carry a domain-qualified URL to learn a
                # vocabulary from, so a memory citing bare routes
                # (`/api/v1/events/presence`, `/admin/macros`) had every
                # one of them stat'd and reported missing. Kept out of
                # `checked` and `missing` — "we looked and it wasn't
                # there" is a meaningless statement about a URL path —
                # but recorded in `dropped_as_route` so the suppression
                # is at least readable off the returned report instead of
                # leaving no trace anywhere. (Leaving no trace is what let
                # 3.25.2's over-broad rule swallow real missing paths.)
                # NOTE: a report whose ONLY non-empty bucket is this one
                # still reaches no MCP surface — see the reach note on
                # `PathDriftReport`.
                #
                # Deliberately LAST in this block: the spaced-bare and
                # ambiguous-truncation arms above must arbitrate first,
                # or a prose-glued candidate (`/tmp/real-dir TCP/IP`)
                # would read as a route on its manufactured tail and skip
                # the prefix-existence fallback that recovers the real
                # path.
                #
                # Appended unguarded, unlike `checked` below. Every value
                # that lands here is a candidate `path`, and
                # `_extract_candidates` already dedupes those (it keys an
                # index on the comparison form and appends only on a
                # miss), so one route cannot arrive twice — a
                # `not in dropped_as_route` guard would be unreachable.
                # `checked` needs its guard for a reason that does not
                # apply here: the spaced-bare arm above appends a DERIVED
                # `prefix`, which a later, genuinely distinct candidate
                # can equal.
                dropped_as_route.append(path)
                continue
        if norm in seen:
            continue
        seen.add(norm)
        checked.append(path)
        if not exists:
            if norm in absent_set:
                expected_absent.append(path)
            else:
                # Known limitation: a bare absolute path that legitimately
                # lives on a REMOTE host (`/opt/gophish`, `/data/compose/.env`
                # on a homelab board) is stat'd against the LOCAL filesystem,
                # so absence here says nothing about the host that owns it.
                # Two outcomes, split by the route rule above:
                #
                #   * Reaches this line and reads `missing` — a perpetual
                #     drift signal — when the citation carries an extension
                #     (`/data/compose/.env`) or its parent happens to exist
                #     locally (`/opt/gophish`, on a host that has `/opt`).
                #   * Never reaches this line at all, since 3.25.2: any
                #     extensionless non-home citation whose parent is absent
                #     locally (`/srv/docker/gitea`, `/mnt/tank/media`) reads
                #     as an application route and is DROPPED — not reported
                #     missing, not even `checked`. It IS recorded in
                #     `dropped_as_route`, which an in-process caller can
                #     read. These shapes produce a route-ONLY report, so
                #     no MCP surface emits them (a report that also
                #     carries drift does ship the bucket via `to_dict()`).
                #
                # Either way `memory_verify(verified_absent_paths=[...])` is
                # the intended escape hatch: it routes the citation to
                # `expected_absent` above and pins it against BOTH outcomes
                # (attested candidates skip the route drop entirely). There
                # is no local-only heuristic that can tell a legitimately-
                # remote path from a genuinely-deleted local one without
                # also suppressing real local drift, so an unattested
                # absence that gets this far stays `missing`.
                missing.append(path)
                if norm in verified_set:
                    # Verified-then-deleted: the caller named this exact
                    # path in `memory_verify(verified_paths=[...])`, so
                    # its absence is evidence about a REVIEWED claim, not
                    # about a token the extractor lifted out of a
                    # sentence. That is the 3-of-3-real class in the
                    # sweep, and the only shape in this loop that earns
                    # escalation. (An absent-attested path never reaches
                    # here — it took the `expected_absent` arm above.)
                    claim_anchored.append(path)
            continue
        if norm in verified_set:
            verified.append(path)
