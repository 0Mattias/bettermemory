"""Write-time credential-shaped-string check for memory_write.

The store is plain-text markdown that `bettermemory sync` pushes across
hosts via git. A memory body that captures a live secret — "remember my
OpenAI key is sk-…", a pasted `AKIA…` line, a private-key PEM block — is a
real footgun: the secret lands unencrypted on disk, in `.events.jsonl`'s
audit trail, and in every clone of the sync repo, and it rots there. The
project's pitch is an *auditable, safe* memory layer; silently persisting a
credential is the opposite of that.

This module is the structural defense, mirroring `durability.py`: a pure
detector the write path calls before anything touches disk.

- `find_credential_markers(body)` returns the hits, or empty if the body
  carries no secret-shaped token. Each hit is a `CredentialMatch(kind,
  snippet)` where `kind` names the detector and `snippet` is a few words of
  surrounding context **with the secret span itself redacted** — the whole
  point is to not re-leak the value, so neither the tool response nor the
  event log ever carries the raw secret.
- `memory_write` runs this FIRST (before the durability/dedup gates). If
  anything fires and `acknowledge_credential` is not set, it returns
  `{status: "credential_warning", markers: [...]}` instead of committing.
  The caller either rewrites the body to describe the secret without
  embedding it ("the deploy uses an AWS key, stored in 1Password") or sets
  `acknowledge_credential=True` for the rare legitimate case (a documented
  public example key, a deliberately-stored test fixture).

Detector discipline (same as durability): precision over recall. A false
positive blocks a legitimate write and trains the user to rubber-stamp the
override; a memory body that merely *mentions* "api_key" or "password" as
prose must never fire. So the detectors are high-confidence vendor-prefixed
token shapes (AWS / OpenAI-Anthropic / GitHub / Slack / Google / Stripe),
the unambiguous private-key PEM header and JWT shape, plus ONE guarded
generic `keyword = <high-entropy value>` rule. Recall is explicitly not the
goal — this is a tripwire for the obvious paste, not a secret scanner.

Telemetry: every fire AND every override is logged to `.events.jsonl` (the
`kind` list only — never the value). A high override rate means a detector
is too loose and should be tightened; the generic rule is the first
suspect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Vendor-prefixed token detectors
# ---------------------------------------------------------------------------
#
# Each entry is (kind, compiled-regex). These match the published *shape* of
# a provider's secret — a fixed prefix plus a fixed-or-bounded body — so the
# false-positive rate is effectively zero: ordinary prose does not contain
# `AKIA` followed by 16 base32 chars. Adding a detector is cheap precisely
# because the shape is unambiguous; resist adding a shapeless "long random
# string" rule, which would fire on hashes, ULIDs, and base64 blobs that are
# perfectly fine to remember.

_PREFIXED_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # AWS access key id (AKIA) / temporary (ASIA): 4-char prefix + 16 base32.
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # OpenAI / Anthropic style: sk-, sk-proj-, sk-ant-… then a long body.
    # Hyphen after sk distinguishes this from Stripe's underscore form. The
    # body may itself contain `-`/`_` (real base64url key bodies do), so to
    # avoid firing on lowercase kebab identifiers ("sk-image-processing-
    # toolkit-v3") the two lookaheads require the body to carry BOTH an
    # uppercase letter and a digit — a high-entropy real key reliably has
    # both; a dictionary-word kebab string does not.
    (
        "openai-anthropic-key",
        re.compile(
            r"\bsk-(?:ant-|proj-)?"
            r"(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[0-9])"
            r"[A-Za-z0-9_-]{20,}\b"
        ),
    ),
    # Stripe secret / restricted key: sk_live_ / rk_test_ + 24+ alnum.
    (
        "stripe-key",
        re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{24,}\b"),
    ),
    # GitHub PATs / OAuth / server / refresh tokens: 4-char prefix + 36.
    (
        "github-token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
    ),
    # GitHub fine-grained PAT.
    ("github-token", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{22,}\b")),
    # Slack tokens: xoxb- / xoxp- / xoxa- / xoxr- / xoxs- plus the
    # browser-client (xoxc-) and rotation/export (xoxe-) families.
    ("slack-token", re.compile(r"\bxox[abceprs]-[0-9A-Za-z-]{10,}\b")),
    # Slack app-level token.
    ("slack-token", re.compile(r"\bxapp-[0-9A-Za-z-]{10,}\b")),
    # Google API key: AIza + 35.
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # PEM private-key header — the unambiguous block opener. One match is
    # enough to flag "you pasted a private key"; we don't scan the body.
    (
        "private-key-pem",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    # JSON Web Token: header.payload.signature, header base64url-starts eyJ.
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
)


# ---------------------------------------------------------------------------
# Guarded generic keyword=value detector
# ---------------------------------------------------------------------------
#
# Catches the config-paste / "my password is …" shape that has no vendor
# prefix. This is the only rule with real false-positive risk, so the VALUE
# is gated hard by `_looks_like_secret` below: a bare keyword next to a word
# ("password rotation is strict", "api_key defaults to None") never fires.

# The value group is captured UNBOUNDED to the next whitespace/quote (not
# capped at some length): the captured span is what `_redact_all` later
# erases, so a cap shorter than the real secret would leave the tail
# un-redacted and leak it into the snippet. Length policy lives entirely in
# `_looks_like_secret` (12–200), which decides whether the captured token is
# a secret at all — never the regex.
_GENERIC_KEYWORD_RE = re.compile(
    r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|"
    r"auth[_-]?token|client[_-]?secret|private[_-]?key|bearer)\b"
    r"\s*(?:[:=]|\bis\b)\s*"
    r"[\"']?(?P<value>[^\s\"']{8,})[\"']?",
    re.IGNORECASE,
)

# Values that look secret-shaped but are conventional placeholders / refs —
# storing these is harmless and blocking them is pure friction.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {
        "none",
        "null",
        "true",
        "false",
        "changeme",
        "redacted",
        "placeholder",
        "example",
        "your_api_key",
        "your-api-key",
        "your_token_here",
        "xxxxxxxxxxxx",
        "************",
        "############",
    }
)


def _looks_like_secret(value: str) -> bool:
    """Heuristic gate for the generic keyword=value rule.

    Precision-first: a real pasted secret is long, high-entropy, mixes
    letters and digits, and isn't an env-var/template reference. Anything
    short, low-diversity, single-word, or a known placeholder is treated as
    prose and lets the write through.
    """
    v = value.strip().strip("\"'")
    # Sentence-final punctuation is part of the prose, not the token: the
    # greedy value group captures "hunter2Abc9XyZ12Q." from "my password is
    # hunter2Abc9XyZ12Q. Rotate it quarterly." — without this strip the
    # trailing dot made the dotted-ref guard below read the secret as a
    # module reference and wave it through. (The redaction span still comes
    # from the regex group, so the extra period gets redacted — harmless.)
    v = v.rstrip(".,;:!?")
    if len(v) < 12 or len(v) > 200:
        return False
    # Env-var / template reference, not a literal secret: $VAR, ${VAR},
    # <your-key>, {{token}}, %SECRET%.
    if v[0] in "$<{%" or v[-1] in ">}%":
        return False
    if v.lower() in _PLACEHOLDER_VALUES:
        return False
    # Structured references the writer legitimately stores — a literal secret
    # is none of these: a filesystem path, a dotted attribute/module ref
    # (`config.SECRET_KEY_V2`, `settings.api_key`), or a SCREAMING_SNAKE
    # constant name (`DEFAULT_API_KEY_V2`). Rejecting them is what keeps the
    # generic rule from firing on `client_secret = config.SECRET_KEY`.
    if "/" in v or "\\" in v:
        return False
    if "." in v and re.fullmatch(r"[A-Za-z_][\w.]*", v):
        return False
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", v):
        return False
    # All-identical run (xxxxxxxx, ********) — a mask, not a secret.
    if len(set(v)) < 8:
        return False
    # A literal secret mixes character classes; a bare dictionary word or a
    # plain number does not.
    has_alpha = any(c.isalpha() for c in v)
    has_digit = any(c.isdigit() for c in v)
    return has_alpha and has_digit


@dataclass(frozen=True)
class CredentialMatch:
    """One credential-shaped hit against a candidate write.

    `kind` names the detector that fired (e.g. ``"aws-access-key-id"``,
    ``"private-key-pem"``, ``"generic-secret-assignment"``). `snippet` is a
    short, one-line slice of surrounding context with EVERY detected secret
    span replaced by a ``[redacted:kind]`` marker — it shows the caller
    where the value sat without echoing the value, so the snippet is safe to
    return to the model and write to the event log.
    """

    kind: str
    snippet: str


_CONTEXT_CHARS = 24


def _redact_all(text: str) -> str:
    """Replace every credential span in `text` with a ``[redacted:kind]``
    placeholder. Used to build leak-free snippets — carving a context window
    from already-redacted text guarantees no raw secret survives into the
    snippet even when two secrets sit within one window.
    """
    spans: list[tuple[int, int, str]] = []
    for kind, regex in _PREFIXED_DETECTORS:
        for m in regex.finditer(text):
            spans.append((m.start(), m.end(), kind))
    for m in _GENERIC_KEYWORD_RE.finditer(text):
        if _looks_like_secret(m.group("value")):
            spans.append(
                (m.start("value"), m.end("value"), "generic-secret-assignment")
            )
    if not spans:
        return text
    # Merge overlapping spans into their union before splicing. Two detectors
    # can match the SAME bytes (e.g. an `api_key = AKIA…` line hits both the
    # AWS detector and the generic rule); splicing both with original offsets
    # would corrupt the output. Union the overlaps (keeping the first kind's
    # label), then splice right-to-left so earlier offsets stay valid.
    spans.sort(key=lambda s: s[0])
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in spans:
        if merged and start < merged[-1][1]:
            p_start, p_end, p_kind = merged[-1]
            merged[-1] = (p_start, max(p_end, end), p_kind)
        else:
            merged.append((start, end, kind))
    out = text
    for start, end, kind in reversed(merged):
        out = f"{out[:start]}[redacted:{kind}]{out[end:]}"
    return out


def _safe_snippet(text: str, start: int, end: int, kind: str) -> str:
    """One-line context window around [start, end), fully redacted.

    Carve ±_CONTEXT_CHARS from the original, then redact every secret span
    inside that window. The redaction runs on the window (not the raw match)
    so an adjacent second secret can't bleed through the padding.

    PEM is special-cased: its detector matches only the `-----BEGIN … KEY-----`
    header, so the bytes AFTER the match are raw key material, not context.
    Suppressing the trailing window for `private-key-pem` keeps those
    key-block bytes out of the snippet (the header alone is the signal).
    """
    s = max(0, start - _CONTEXT_CHARS)
    trailing = 0 if kind == "private-key-pem" else _CONTEXT_CHARS
    e = min(len(text), end + trailing)
    window = _redact_all(text[s:e])
    window = re.sub(r"\s+", " ", window.replace("\n", " ")).strip()
    prefix = "..." if s > 0 else ""
    suffix = "..." if e < len(text) else ""
    return f"{prefix}{window}{suffix}"


def find_credential_markers(content: str) -> list[CredentialMatch]:
    """Scan `content` for credential-shaped tokens.

    Returns a list of `CredentialMatch`; an empty list means no secret shape
    was found and the body is safe to persist. Hits are deduplicated by
    `kind`: a body pasting three AWS keys reports `aws-access-key-id` once,
    with the first (redacted) snippet — enough to tell the caller "you put a
    secret in here," without enumerating every occurrence.
    """
    hits: list[CredentialMatch] = []
    seen: set[str] = set()

    def _add(kind: str, start: int, end: int) -> None:
        if kind in seen:
            return
        hits.append(
            CredentialMatch(kind=kind, snippet=_safe_snippet(content, start, end, kind))
        )
        seen.add(kind)

    for kind, regex in _PREFIXED_DETECTORS:
        match = regex.search(content)
        if match is not None:
            _add(kind, match.start(), match.end())

    for match in _GENERIC_KEYWORD_RE.finditer(content):
        if "generic-secret-assignment" in seen:
            break
        if _looks_like_secret(match.group("value")):
            _add("generic-secret-assignment", match.start("value"), match.end("value"))

    return hits


__all__ = [
    "CredentialMatch",
    "find_credential_markers",
]
