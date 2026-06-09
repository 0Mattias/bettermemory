"""Unit tests for credentials.py — the secret-shaped-token detector.

Two contracts matter and both are pinned here:

1. Precision. A body that merely *mentions* credential vocabulary as prose
   ("the api_key config defaults to None", "password rotation is 90 days")
   must never fire — a false positive blocks a legitimate write and trains
   the user to rubber-stamp the override.
2. Redaction. When a detector fires, the `snippet` it returns must NOT
   contain the raw secret — the whole point is to keep the value out of the
   tool response and the event log.

Fixture note: every secret-SHAPED fixture is assembled from fragments via
`_shaped(...)`, so the complete token literal never appears in this file's
source. A credential detector's own fixtures otherwise trip push-protection
secret scanners (GitHub's included) and block the push — the scanner can't
tell a test fixture from a real leaked key. Splitting the literal keeps the
runtime value identical while leaving nothing for a content scanner to match.
"""

from __future__ import annotations

import pytest

from bettermemory.credentials import find_credential_markers


def _shaped(*parts: str) -> str:
    """Join fragments into a secret-shaped value with no scannable literal."""
    return "".join(parts)


# Public AWS example key (a documented shape fixture, never a live key) plus
# one synthetic fixture per detector — all fragment-assembled (see module docstring).
_AWS = _shaped("AKIA", "IOSFODNN7EXAMPLE")
_ASIA = _shaped("ASIA", "Y34FZKBOKMUTVV7A")
_OPENAI = _shaped("sk-", "abcdEFGH1234ijklMNOP5678")
_ANTHROPIC = _shaped("sk-ant-", "api03-AAAAbbbbCCCCddddEEEE1234")
_STRIPE = _shaped("sk_", "live_4eC39HqLyjWDarjtT1zdp7dc")
_GITHUB = _shaped("ghp_", "1234567890abcdefABCDEF1234567890abcd")
_GITHUB_PAT = _shaped("github_pat_", "11ABCDEFG0abcdefghij_klmnopqrstuvwxyz123456")
_SLACK = _shaped("xoxb-", "1234567890-ABCDEFGHIJKLMNOP")
_GOOGLE = _shaped("AIza", "SyA1234567890abcdefghijklmnopqrstuv")
_PEM_HEADER = _shaped("-----BEGIN OPENSSH ", "PRIVATE KEY-----")
_JWT = _shaped(
    "eyJ",
    "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
)


# ---------------------------------------------------------------------------
# Negative cases — durable prose must never trip the check
# ---------------------------------------------------------------------------


def test_empty_body_no_markers() -> None:
    assert find_credential_markers("") == []


def test_durable_prose_no_markers() -> None:
    body = (
        "The auth service uses JWT with rotating refresh tokens. The access "
        "token TTL is 5 minutes and the api_key for the upstream is stored "
        "in 1Password, not here."
    )
    assert find_credential_markers(body) == []


def test_keyword_without_secret_value_does_not_fire() -> None:
    """`password rotation is strict` mentions the keyword but no secret."""
    for body in (
        "The api_key config option defaults to None.",
        "Password rotation policy is every 90 days.",
        "The secret sauce is good documentation.",
        "Our auth token strategy is short-lived bearer tokens.",
    ):
        assert find_credential_markers(body) == [], body


def test_placeholder_values_do_not_fire() -> None:
    for body in (
        "api_key = your_api_key",
        "password = changeme",
        "secret: REDACTED",
        "token = ${GITHUB_TOKEN}",
        "api_key = $OPENAI_API_KEY",
        "password = <your-password-here>",
        "secret = xxxxxxxxxxxx",
    ):
        assert find_credential_markers(body) == [], body


def test_low_diversity_value_does_not_fire() -> None:
    """A short or repetitive value isn't a real secret."""
    assert find_credential_markers("password = abc123") == []  # too short
    assert find_credential_markers("api_key = aaaa1111aaaa") == []  # <8 distinct


def test_plain_hex_or_ulid_does_not_fire() -> None:
    """Hashes, commit SHAs, ULIDs are fine to remember — no keyword, and the
    generic rule needs a keyword anchor."""
    body = "Memory id 01KSKHT5T9EJS5FZRWSSCFQ2PK was verified at sha c6b3277."
    assert find_credential_markers(body) == []


# ---------------------------------------------------------------------------
# Positive cases — one per detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("aws-access-key-id", f"Prod key: {_AWS} for the deploy role."),
        ("aws-access-key-id", f"{_ASIA} is the temp session key."),
        ("openai-anthropic-key", f"export OPENAI={_OPENAI}"),
        ("openai-anthropic-key", f"anthropic {_ANTHROPIC}"),
        ("stripe-key", f"stripe {_STRIPE} here"),
        ("github-token", f"{_GITHUB} token"),
        ("github-token", f"{_GITHUB_PAT}"),
        ("slack-token", f"slack {_SLACK} webhook"),
        ("google-api-key", f"maps {_GOOGLE} key"),
        ("private-key-pem", f"{_PEM_HEADER}\nb3BlbnNzaC1rZXk=\n"),
        ("jwt", f"Authorization: Bearer {_JWT}"),
        ("generic-secret-assignment", "password = hunter2Abc9XyZ12Q"),
    ],
)
def test_detector_fires(kind: str, body: str) -> None:
    hits = find_credential_markers(body)
    kinds = {h.kind for h in hits}
    assert kind in kinds, f"{kind} did not fire on: {body!r} (got {kinds})"


# ---------------------------------------------------------------------------
# Redaction — the snippet must never echo the secret back
# ---------------------------------------------------------------------------


def test_snippet_redacts_the_secret() -> None:
    body = f"The prod AWS access key is {_AWS} — keep it safe."
    hits = find_credential_markers(body)
    assert hits
    snippet = hits[0].snippet
    assert _AWS not in snippet
    assert "[redacted:aws-access-key-id]" in snippet
    # Surrounding context is preserved so the caller knows where it was.
    assert "prod AWS access key" in snippet


def test_snippet_redacts_generic_value() -> None:
    secret = "hunter2Abc9XyZ12Q"
    hits = find_credential_markers(f"db password = {secret} (prod)")
    assert hits
    snippet = hits[0].snippet
    assert secret not in snippet
    # The keyword stays (useful), the value is masked.
    assert "password" in snippet
    assert "[redacted:generic-secret-assignment]" in snippet


def test_adjacent_second_secret_does_not_bleed_into_snippet() -> None:
    """Two secrets within one context window: neither raw value survives."""
    body = f"{_AWS} {_OPENAI}"
    hits = find_credential_markers(body)
    for h in hits:
        assert _AWS not in h.snippet
        assert _OPENAI not in h.snippet


# ---------------------------------------------------------------------------
# Dedup by kind
# ---------------------------------------------------------------------------


def test_dedup_by_kind() -> None:
    """Three AWS keys report `aws-access-key-id` once, not three times."""
    body = f"{_AWS} and {_shaped('AKIA', 'IOSFODNN7EXAMPL2')} and " + _shaped(
        "AKIA", "IOSFODNN7EXAMPL3"
    )
    hits = find_credential_markers(body)
    aws = [h for h in hits if h.kind == "aws-access-key-id"]
    assert len(aws) == 1


# ---------------------------------------------------------------------------
# Regression: review findings (3.8.0 adversarial pass)
# ---------------------------------------------------------------------------


def test_long_generic_value_fully_redacted() -> None:
    """A generic-assignment secret longer than 80 chars must be redacted in
    full — no raw-secret window survives into the snippet. (The detector's
    value group was once capped at 80 while the redaction span followed it,
    leaking the tail.)"""
    secret = "Zx9Kq7Wm2Pv5Bn8Lt4Rd6Fy1Gh3Jc0Aa2Bb4Cc6Dd8Ee1Ff3Gg5Hh7Ii9Jj0Kk2Ll4Mm6Nn8Oo1Pp3Qq5"
    assert len(secret) > 80
    body = f"db password = {secret} (prod)"
    hits = find_credential_markers(body)
    assert hits
    snippet = hits[0].snippet
    assert secret not in snippet
    # No 10-char contiguous window of the secret may survive.
    for i in range(0, len(secret) - 10):
        assert secret[i : i + 10] not in snippet, f"leaked window at offset {i}"
    assert "[redacted:generic-secret-assignment]" in snippet


def test_pem_key_block_not_in_snippet() -> None:
    """The PEM detector matches only the header; the key-block bytes after it
    must not bleed into the snippet's trailing context window."""
    body = (
        f"ssh key:\n{_PEM_HEADER}\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    hits = find_credential_markers(body)
    pem = [h for h in hits if h.kind == "private-key-pem"]
    assert pem
    snippet = pem[0].snippet
    assert "b3BlbnNzaC1rZXktdjEAAAA" not in snippet
    assert "[redacted:private-key-pem]" in snippet


def test_sk_kebab_identifier_does_not_fire() -> None:
    """Lowercase kebab identifiers that happen to start `sk-` are not keys —
    real OpenAI/Anthropic keys carry an uppercase letter AND a digit."""
    for body in (
        "We use the sk-image-processing-toolkit-v3 service for thumbnails.",
        "The sk-learn-preprocessing-pipeline module handles scaling.",
        "deploy sk-event-bus-consumer-group-v2 to staging.",
    ):
        kinds = {h.kind for h in find_credential_markers(body)}
        assert "openai-anthropic-key" not in kinds, body


def test_code_config_references_do_not_fire() -> None:
    """Dotted attribute refs, paths, and SCREAMING_SNAKE constant names are
    common durable content, not literal secrets."""
    for body in (
        "client_secret = config.SECRET_KEY_V2",
        "api_key = settings.DEFAULT_API_KEY_V2",
        "secret = DEFAULT_API_KEY_V2",
        "password = /etc/secrets/db_password",
    ):
        assert find_credential_markers(body) == [], body


def test_redact_all_merges_overlapping_spans() -> None:
    """When two detectors match the same span (`api_key = AKIA…` hits both the
    AWS detector and the generic rule), the redaction must not corrupt the
    output — exactly one clean marker, no leaked tail."""
    from bettermemory.credentials import _redact_all

    body = f"api_key = {_AWS}"
    out = _redact_all(body)
    assert _AWS not in out
    assert out.count("[redacted:") == 1


# Regression: extractor false-signal hunt (2026-06-09 multi-agent audit)


def test_sentence_final_period_does_not_mask_secret() -> None:
    """HIGH finding: the greedy value group captures the sentence period,
    and the dotted-ref guard then read the secret as a module reference."""
    body = "My Grafana admin password is hunter2Abc9XyZ12Q. Rotate it quarterly."
    kinds = [m.kind for m in find_credential_markers(body)]
    assert "generic-secret-assignment" in kinds


def test_dotted_module_ref_at_sentence_end_still_passes() -> None:
    body = "The client_secret = config.SECRET_KEY_V2. See settings module."
    assert find_credential_markers(body) == []


def test_encrypted_pkcs8_pem_header_fires() -> None:
    body = "-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIFHDBOBgkqhkiG9w0BBQ0w"
    kinds = [m.kind for m in find_credential_markers(body)]
    assert "private-key-pem" in kinds


def test_slack_app_and_client_token_families_fire() -> None:
    # Fixtures are assembled at runtime so the raw source file never
    # contains a contiguous secret-shaped token — GitHub's secret-scanning
    # push protection scans blobs and rejects pushes carrying full
    # Slack-token shapes, fake or not.
    for prefix, body in (
        ("xapp-", "1-A0XXXXXXX-1234567890123-abcdefabcdefabcdef"),
        ("xoxc-", "1234567890-abcdefghijklmnop"),
        ("xoxe-", "1-abcdefghijklmnopqrstuvwx"),
    ):
        token = prefix + body
        kinds = [m.kind for m in find_credential_markers(f"token is {token} ok")]
        assert "slack-token" in kinds, prefix
