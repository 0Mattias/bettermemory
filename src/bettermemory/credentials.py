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
token shapes (AWS / OpenAI-Anthropic / GitHub / Slack / Google / Stripe /
SendGrid / Vault), the unambiguous private-key PEM header and JWT shape,
plus two guarded value rules — the generic `keyword = <high-entropy value>`
assignment and the connection-URI userinfo password — both gated by
`_looks_like_secret`. Recall is explicitly not the goal — this is a
tripwire for the obvious paste, not a secret scanner.

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
# false-positive rate on ordinary prose is effectively zero: it does not
# contain `AKIA` followed by 16 base32 chars. The residual FP class is
# masked/placeholder spellings of the shapes THEMSELVES ("AKIA" + 16 X's in
# incident notes, the Slack docs placeholder) — handled by the digit
# lookaheads and the `_is_masked_token` guard below, never by loosening the
# shapes. Adding a detector is cheap precisely because the shape is
# unambiguous; resist adding a shapeless "long random string" rule, which
# would fire on hashes, ULIDs, and base64 blobs that are perfectly fine to
# remember.

_PREFIXED_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # AWS access key id (AKIA) / temporary (ASIA): 4-char prefix + 16 base32.
    # The named `body` group feeds the `_is_masked_token` guard.
    (
        "aws-access-key-id",
        re.compile(r"\b(?:AKIA|ASIA)(?P<body>[0-9A-Z]{16})\b"),
    ),
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
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_(?P<body>[A-Za-z0-9]{36})\b"),
    ),
    # GitHub fine-grained PAT.
    ("github-token", re.compile(r"\bgithub_pat_(?P<body>[0-9A-Za-z_]{22,})\b")),
    # Slack tokens: xoxb- / xoxp- / xoxa- / xoxr- / xoxs- plus the
    # browser-client (xoxc-) and rotation/export (xoxe-) families. Real
    # Slack tokens always embed numeric workspace/team IDs, so the digit
    # lookahead rejects the docs placeholder ("xoxb-your-bot-token") the
    # same way the sk- lookaheads above reject kebab identifiers.
    (
        "slack-token",
        re.compile(r"\bxox[abceprs]-(?=[0-9A-Za-z-]*[0-9])[0-9A-Za-z-]{10,}\b"),
    ),
    # Slack app-level token.
    (
        "slack-token",
        re.compile(r"\bxapp-(?=[0-9A-Za-z-]*[0-9])[0-9A-Za-z-]{10,}\b"),
    ),
    # Google API key: AIza + 35.
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # SendGrid API key: SG. + 22-char id + 43-char secret. Dot-delimited by
    # design (like JWT below), so the generic rule's dotted-ref guard reads
    # it as an attribute reference — it needs a dedicated shape detector.
    (
        "sendgrid-api-key",
        re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"),
    ),
    # HashiCorp Vault tokens: hvs. (service) / hvb. (batch) / hvr.
    # (recovery) — dotted like SendGrid, same rationale.
    ("vault-token", re.compile(r"\bhv[sbr]\.[A-Za-z0-9_-]{24,}\b")),
    # PEM private-key header — the unambiguous block opener. One match is
    # enough to flag "you pasted a private key"; we don't scan the body.
    # The zero-width lookahead requires a following line of base64 key
    # material (or a Proc-Type:/DEK-Info: encapsulation header), so prose
    # that merely QUOTES the header to describe a key format ("files begin
    # '-----BEGIN … KEY-----'") does not fire. match.end() stays at the
    # header, keeping snippet/redaction behavior unchanged.
    (
        "private-key-pem",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"
            r"(?=[ \t]*\r?\n(?:[A-Za-z0-9+/=_-]{16,}|(?:Proc-Type|DEK-Info):))"
        ),
    ),
    # JSON Web Token: header.payload.signature, header base64url-starts eyJ.
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
)

# Bounded-body detectors above name their token body `body` so masked /
# already-redacted spellings can be skipped: incident notes routinely carry
# "AKIA" + 16 X's or "ghp_" + an x-run — a mask, not a secret (the exact
# mirror of the generic rule's len(set(v)) diversity guard). A real random
# token body of 16+ chars has far more than 5 distinct characters, so the
# recall cost is nil. Both match sites (find_credential_markers and
# _redact_all) MUST share this guard so detection and redaction agree.
_MIN_BODY_DIVERSITY = 5


def _is_masked_token(match: re.Match[str]) -> bool:
    body = match.groupdict().get("body")
    return body is not None and len(set(body)) < _MIN_BODY_DIVERSITY


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
# `_looks_like_secret` (12–1024), which decides whether the captured token is
# a secret at all — never the regex.
#
# Anchor anatomy (precision lives in `_looks_like_secret`, never here):
# - `(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,30}[_-]){0,5}` admits env-var /
#   identifier prefixes (POSTGRES_PASSWORD=, aws_secret_access_key=) — the
#   .env / docker-compose paste is this rule's primary target. The
#   repetition is BOUNDED as ReDoS defense: the unbounded spelling
#   `(?:[A-Za-z0-9]+[_-])*` backtracks quadratically across the alternation
#   boundary on any dense `_`/`-`-separated run — 4x cost per size doubling,
#   tens of seconds by ~200KB, with NO keyword required — a synchronous
#   event-loop hang reachable under the 1MB body cap. The bound is lossless
#   for recall: `_`/`-` are not alnum, so the lookbehind lets a match start
#   right after ANY separator — a prefix with more segments (or a longer
#   single segment) than the bound simply anchors closer to the keyword.
# - Compound keywords accept a literal space (memory bodies are prose:
#   "my Mailgun api key is …"); `secret` extends to SECRET_KEY /
#   secret_access_key. Trailing compounds (SECRET_KEY_BASE=) still miss —
#   only prefixes were opened up.
# - The wrapper class after the keyword admits quoted / markdown-formatted
#   keys ('"password":', '**Password**:') from pasted JSON/YAML/notes.
# - The separator covers code assignment operators (:=, =>, ==) and the
#   conversational connectives ("is set to", "was changed to", "is now").
# - `authorization\s*:\s*bearer` anchors the canonical HTTP-header paste
#   (curl lines), whose separator sits BEFORE the keyword.
_GENERIC_KEYWORD_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,30}[_-]){0,5}"
    r"(?:password|passwd|secret(?:[_-]?(?:access[_-]?)?key)?|api[ _-]?key|"
    r"access[ _-]?token|auth[ _-]?token|client[ _-]?secret|private[ _-]?key|"
    r"bearer(?:[_-]token)?)\b[\"'*`]*"
    r"\s*(?:=>|[:=]=?|\b(?:is|was)(?:\s+(?:set|changed|updated|reset|rotated))?"
    r"(?:\s+(?:to|now))?\b)\s*"
    r"|\bauthorization\s*:\s*bearer\s+"
    r")"
    r"[\"']?(?P<value>[^\s\"']{8,})[\"']?",
    re.IGNORECASE,
)

# Connection-URI userinfo password: `scheme://user:password@host` — the
# classic DATABASE_URL paste (postgres://, redis://, amqp://). It carries a
# live password with no vendor prefix and no keyword adjacent to the value,
# so no other rule can see it. The `scheme://user:` anchor is as
# structurally unambiguous as a vendor prefix, and the captured password is
# still gated by `_looks_like_secret`, so docs placeholders
# (postgres://user:password@host) and template refs (${DB_PASS}) stay
# silent. RFC 3986 forbids a raw "/" in userinfo, so excluding it from the
# value class is correct — and keeps the captured password clear of the
# path-shape guard.
_URI_USERINFO_RE = re.compile(
    r"\b[a-z][a-z0-9+.\-]{1,30}://[^\s:/@\"']{1,64}:(?P<value>[^\s/@\"']{8,})@",
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

# Word stems that — joined by -/_ separators and digit runs — form the
# conventional documentation placeholders real sample configs use
# (dummy_password_123, test_api_key_12345, replace-me-123). A value is
# placeholder-shaped when EVERY separated, digit-stripped token is one of
# these; a real high-entropy secret cannot decompose into dictionary
# placeholder words.
_PLACEHOLDER_WORDS: frozenset[str] = frozenset(
    {
        "api",
        "auth",
        "change",
        "changeme",
        "demo",
        "dev",
        "dummy",
        "example",
        "fake",
        "here",
        "key",
        "local",
        "me",
        "passwd",
        "password",
        "placeholder",
        "replace",
        "sample",
        "secret",
        "test",
        "token",
        "your",
    }
)


# ISO-8601 datetime shape, extended (2026-01-15T00:30:00Z,
# 2026-06-01T09:30:00+02:00) and basic/compact (20260115T003000Z) forms.
# The connective separators make `to` optional ("password was rotated <when>"
# is natural prose), so a following timestamp gets captured as the "value" —
# but a datetime is rotation METADATA, not a secret, and it sails through the
# mixed-class checks (digits + T/Z, 8+ distinct chars). The shape is as
# structurally unambiguous as a vendor prefix, so rejecting a fullmatch costs
# zero recall: no real secret is spelled exactly digits-dashes-colons with a
# single mid-string T. (Date-only forms never reach this guard — at 10 chars
# they fail the length floor.)
_DATETIME_SHAPE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:[Zz]|[+-]\d{2}:?\d{2})?"
    r"|\d{8}[Tt]\d{4,6}(?:[Zz]|[+-]\d{2}:?\d{2})?"
)


def _is_placeholder(v: str) -> bool:
    """True when `v` is a conventional placeholder, not a secret.

    Exact allowlist membership, plus the digit-suffixed spellings sample
    configs actually use (changeme12345), plus values that decompose
    entirely into placeholder words and digits (dummy_password_123).
    """
    lowered = v.lower()
    if lowered in _PLACEHOLDER_VALUES:
        return True
    if re.sub(r"[-_]?\d+$", "", lowered) in _PLACEHOLDER_VALUES:
        return True
    tokens = [t.rstrip("0123456789") for t in re.split(r"[-_]+", lowered)]
    return all(not t or t in _PLACEHOLDER_WORDS for t in tokens)


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
    # A trailing closer is template evidence only when its opener (or an
    # earlier %) appears in the value ("myapp-${VAR}", "<your-key>",
    # "%SECRET%"). A LONE trailing closer is surrounding syntax — the end
    # of a YAML flow mapping or JS object literal ("{…, password: hunterX}")
    # — so strip it before judging the token underneath.
    if v.endswith("}") and "{" not in v:
        v = v[:-1]
    elif v.endswith(">") and "<" not in v:
        v = v[:-1]
    elif v.endswith("%") and "%" not in v[:-1]:
        v = v[:-1]
    # Length floor: anything under 12 chars is prose-plausible. The CEILING
    # is deliberately generous (1024, not the old 200): a very long
    # whitespace-free token sitting after a credential keyword separator is
    # MORE suspicious, not less — a 240-char base64 paste after `api_key =`
    # is exactly the config-paste this rule targets, and the old cap waved
    # it through. The value class already excludes whitespace, so a prose
    # paragraph after "password is" can never reach here; the bound only
    # stops pathological single-token blobs (a base64-encoded *file*, a
    # minified-code fragment) from reading as a credential.
    if len(v) < 12 or len(v) > 1024:
        return False
    # Env-var / template reference, not a literal secret: $VAR, ${VAR},
    # <your-key>, {{token}}, %SECRET%.
    if v[0] in "$<{%" or v[-1] in ">}%":
        return False
    if _is_placeholder(v):
        return False
    # Structured references the writer legitimately stores: a filesystem
    # path, a URL, a dotted attribute/module ref (`config.SECRET_KEY_V2`),
    # a call expression (`secrets.token_hex(32)`), or a SCREAMING_SNAKE
    # constant name (`DEFAULT_API_KEY_V2`). Rejecting them is what keeps
    # the generic rule from firing on `client_secret = config.SECRET_KEY`.
    # (Dotted VENDOR secrets — JWT, SendGrid SG.x.y, Vault hvs.… — are real
    # exceptions to "a literal secret is never dotted"; they are caught by
    # their dedicated prefixed detectors, never by this rule.)
    if "\\" in v:
        return False
    if "/" in v:
        # Path-SHAPED, not merely slash-bearing: standard base64 uses "/"
        # in its alphabet, so a mid-string slash alone must not reject.
        if re.match(r"(?:\.{0,2}/|~/|[A-Za-z]:/)", v):
            return False
        if "://" in v:
            return False
        if re.fullmatch(r"[a-z0-9_.\-]+(?:/[a-z0-9_.\-]+)+", v):
            return False
    if "." in v and re.fullmatch(r"[A-Za-z_][\w.]*", v):
        return False
    # Code call expression (`secrets.token_hex(32)`, `str(uuid4())`) — a
    # structured reference like the dotted form above, not a literal: a
    # pasted high-entropy literal never has identifier-then-parenthesized-
    # args shape.
    if re.fullmatch(r"[A-Za-z_][\w.]*\(.*\)", v):
        return False
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", v):
        return False
    # Timestamp, not a token: "the password was rotated 2026-01-15T00:30:00Z"
    # records WHEN, not WHAT — see _DATETIME_SHAPE_RE.
    if _DATETIME_SHAPE_RE.fullmatch(v):
        return False
    # All-identical run (xxxxxxxx, ********) — a mask, not a secret.
    if len(set(v)) < 8:
        return False
    # Separator-bearing all-lowercase values are technical descriptors or
    # kebab/snake identifiers ("sha256-hashed", "base64-encoded",
    # "expire_after_30d", "bcrypt_hash_v2"), not secrets — the same
    # rationale as the sk- detector's lookaheads: a real high-entropy
    # secret reliably carries an uppercase letter.
    if ("-" in v or "_" in v) and not any(c.isupper() for c in v):
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


def _merged_secret_spans(text: str) -> list[tuple[int, int, str]]:
    """Every credential span in `text` as merged (start, end, kind) tuples.

    Overlapping spans are unioned before splicing. Two detectors can match
    the SAME bytes (e.g. an `api_key = AKIA…` line hits both the AWS
    detector and the generic rule); splicing both with original offsets
    would corrupt the output. Union the overlaps (keeping the first kind's
    label) so callers can splice right-to-left with valid offsets.
    """
    spans: list[tuple[int, int, str]] = []
    for kind, regex in _PREFIXED_DETECTORS:
        for m in regex.finditer(text):
            if _is_masked_token(m):
                continue
            spans.append((m.start(), m.end(), kind))
    for m in _GENERIC_KEYWORD_RE.finditer(text):
        if _looks_like_secret(m.group("value")):
            spans.append(
                (m.start("value"), m.end("value"), "generic-secret-assignment")
            )
    for m in _URI_USERINFO_RE.finditer(text):
        if _looks_like_secret(m.group("value")):
            spans.append((m.start("value"), m.end("value"), "connection-uri-password"))
    spans.sort(key=lambda s: s[0])
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in spans:
        if merged and start < merged[-1][1]:
            p_start, p_end, p_kind = merged[-1]
            merged[-1] = (p_start, max(p_end, end), p_kind)
        else:
            merged.append((start, end, kind))
    return merged


def _redact_all(text: str) -> str:
    """Replace every credential span in `text` with a ``[redacted:kind]``
    placeholder. Used to build leak-free snippets — no raw secret survives
    into the snippet even when two secrets sit within one window.
    """
    out = text
    for start, end, kind in reversed(_merged_secret_spans(text)):
        out = f"{out[:start]}[redacted:{kind}]{out[end:]}"
    return out


def _safe_snippet(text: str, start: int, end: int, kind: str) -> str:
    """One-line context window around [start, end), fully redacted.

    Spans are detected on the FULL text, then spliced into the carved
    ±_CONTEXT_CHARS window (clipped at its edges). Carving first and
    re-detecting inside the window would lose any match whose required
    context falls outside the window — the PEM detector's key-material
    lookahead, or a secret whose tail pokes past the padding.

    PEM is special-cased: its detector matches only the `-----BEGIN … KEY-----`
    header, so the bytes AFTER the match are raw key material, not context.
    Suppressing the trailing window for `private-key-pem` keeps those
    key-block bytes out of the snippet (the header alone is the signal).
    """
    s = max(0, start - _CONTEXT_CHARS)
    trailing = 0 if kind == "private-key-pem" else _CONTEXT_CHARS
    e = min(len(text), end + trailing)
    window = text[s:e]
    for sp_start, sp_end, sp_kind in reversed(_merged_secret_spans(text)):
        if sp_end <= s or sp_start >= e:
            continue
        w_start = max(sp_start - s, 0)
        w_end = min(sp_end - s, len(window))
        window = f"{window[:w_start]}[redacted:{sp_kind}]{window[w_end:]}"
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
        # First NON-MASK hit per detector: an x-run or all-X mask is an
        # already-redacted shape, not a secret (see _is_masked_token).
        for match in regex.finditer(content):
            if _is_masked_token(match):
                continue
            _add(kind, match.start(), match.end())
            break

    for match in _GENERIC_KEYWORD_RE.finditer(content):
        if "generic-secret-assignment" in seen:
            break
        if _looks_like_secret(match.group("value")):
            _add("generic-secret-assignment", match.start("value"), match.end("value"))

    for match in _URI_USERINFO_RE.finditer(content):
        if "connection-uri-password" in seen:
            break
        if _looks_like_secret(match.group("value")):
            _add("connection-uri-password", match.start("value"), match.end("value"))

    return hits


__all__ = [
    "CredentialMatch",
    "find_credential_markers",
]
