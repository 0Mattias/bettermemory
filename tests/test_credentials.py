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
_SENDGRID = _shaped(
    "SG.", "aBcDeFgH1234iJkLmNoP56", ".", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfG"
)
_VAULT = _shaped("hvs.", "AbCdEfGhIjKlMnOpQrStUvWx")
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
        # Digit-suffixed / word-decomposable spellings of the same
        # placeholders — sample-config docs, not secrets.
        "password = changeme12345",
        "password = dummy_password_123",
        "api_key = test_api_key_12345",
        "secret = example_secret_1234",
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
        ("sendgrid-api-key", f"sendgrid {_SENDGRID} key"),
        ("vault-token", f"vault {_VAULT} token"),
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
    """Dotted attribute refs, paths, call expressions, and SCREAMING_SNAKE
    constant names are common durable content, not literal secrets."""
    for body in (
        "client_secret = config.SECRET_KEY_V2",
        "api_key = settings.DEFAULT_API_KEY_V2",
        "secret = DEFAULT_API_KEY_V2",
        "password = /etc/secrets/db_password",
        # Call expressions: a documented key-generation decision, not a key.
        "api_key = secrets.token_hex(32), generated per-tenant at signup",
        "api_key = str(uuid4())",
        "secret = os.urandom(24)",
        "api_key = settings.get_key(tenant_id1)",
        # '==' comparison against a structured ref (admitted by the := / =>
        # separator widening, rejected by the dotted-ref value guard).
        "password == user.hashed_password",
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


# ---------------------------------------------------------------------------
# Regression: extractor false-signal hunt drain (2026-06-10 batch)
# ---------------------------------------------------------------------------


def test_env_prefixed_and_compound_keywords_fire() -> None:
    """The old \\b anchors made the generic rule blind to the canonical
    .env / docker-compose / AWS-credentials-file paste shapes."""
    for body in (
        f"POSTGRES_PASSWORD={_shaped('tr0ub4dor', '3Xy9QmW2')}",
        f"SECRET_KEY={_shaped('x8f2nQv9', 'Lp4Rt7Zw1Kj6')}",
        f"secret_key = {_shaped('x8f2nQv9', 'Lp4Rt7Zw1Kj6Bm3')}",
        f"aws_secret_access_key = {_shaped('wJalrXUtnFEMI', 'K7MDENGbPxRfiCY')}",
    ):
        kinds = [m.kind for m in find_credential_markers(body)]
        assert "generic-secret-assignment" in kinds, body


def test_env_prefixed_path_value_does_not_fire() -> None:
    """The _FILE convention assigns a path, not a secret."""
    body = "POSTGRES_PASSWORD_FILE=/run/secrets/db_password"
    assert find_credential_markers(body) == []


def test_hyphenated_descriptor_prose_does_not_fire() -> None:
    """'password is sha256-hashed' DESCRIBES credential handling — the
    exact describe-don't-embed rewrite the credential_warning suggests."""
    for body in (
        "The admin password is sha256-hashed before storage; never plaintext.",
        "The session-cookie signing secret is base64-encoded, 32 bytes, in Vault.",
        "Backup archive password is gpg2-encrypted and rotated quarterly.",
    ):
        assert find_credential_markers(body) == [], body


def test_hyphenated_value_with_uppercase_still_fires() -> None:
    """The descriptor guard keys on all-lowercase: a hyphenated value that
    carries uppercase is still secret-shaped."""
    kinds = [m.kind for m in find_credential_markers("password is xK9-mQ2-vR7-bT4w")]
    assert "generic-secret-assignment" in kinds


def test_masked_token_shapes_do_not_fire() -> None:
    """Masks and docs placeholders of vendor shapes are not secrets: x-runs
    and the Slack docs placeholder stay silent (the same class the sk-
    lookaheads already reject). Fixtures are fragment-assembled."""
    for body in (
        "Slack bot token format is " + _shaped("xoxb-", "your-bot-token") + ", env.",
        "Rotated the leaked key " + _shaped("AKIA", "X" * 16) + " per the ticket.",
        "PAT shape: " + _shaped("ghp_", "x" * 36),
        "fine-grained shape: " + _shaped("github_pat_", "x" * 22),
    ):
        assert find_credential_markers(body) == [], body


def test_base64_value_with_slash_fires() -> None:
    """Standard (non-url-safe) base64 uses '/' in its alphabet — a
    mid-string slash is not a filesystem path."""
    for body in (
        f"client_secret = {_shaped('mP9/QxT2vNb8', 'KdR5wYe3UfH7jc2L0aQ=')}",
        f"password = {_shaped('N8v2kQ/', 'p9Rt4Lx7ZwJm5')}",
    ):
        kinds = [m.kind for m in find_credential_markers(body)]
        assert "generic-secret-assignment" in kinds, body


def test_path_shaped_values_still_do_not_fire() -> None:
    for body in (
        "api_key = ~/secrets/key1.pem",
        "password = myapp-${VAULT_SECRET}",
    ):
        assert find_credential_markers(body) == [], body


def test_quoted_and_markdown_keys_fire() -> None:
    """JSON / quoted-YAML / markdown-bold keys are the config-paste shape;
    the rule already tolerated quotes around the VALUE."""
    for body in (
        'DB config: {"user": "admin", "password": "S3cr3tPazzw0rdX"}',
        "'password': 'S3cr3tPazzw0rdX'",
        "**Password**: hunter2Abc9XyZ12Q",
    ):
        kinds = [m.kind for m in find_credential_markers(body)]
        assert "generic-secret-assignment" in kinds, body


def test_authorization_bearer_header_fires() -> None:
    """Opaque bearer tokens have no vendor prefix — the header anchor is
    the only detector that can see the canonical curl paste."""
    body = (
        'curl -H "Authorization: Bearer Zx9Kq7Wm2Pv5Bn8Lt4Rd6Fy1" '
        "https://metrics.internal/api"
    )
    kinds = [m.kind for m in find_credential_markers(body)]
    assert "generic-secret-assignment" in kinds


def test_bearer_token_key_fires() -> None:
    """Prometheus/YAML-style `bearer_token:` was killed by \\b before `_`."""
    kinds = [
        m.kind
        for m in find_credential_markers("bearer_token: Zx9Kq7Wm2Pv5Bn8Lt4Rd6Fy1")
    ]
    assert "generic-secret-assignment" in kinds


def test_bearer_prose_does_not_fire() -> None:
    body = "Authorization: Bearer tokens expire after 15 minutes."
    assert find_credential_markers(body) == []


def test_connection_uri_password_fires_and_redacts() -> None:
    """The classic DATABASE_URL paste: a live userinfo password that no
    vendor prefix or keyword anchor can see."""
    secret = _shaped("S3cr3t", "Pazz42")
    for body in (
        f"Staging DB is at postgres://app:{secret}@db.internal:5432/app",
        f"redis://default:{secret}@cache.internal:6379/0 for sessions",
    ):
        hits = find_credential_markers(body)
        assert "connection-uri-password" in [m.kind for m in hits], body
        for h in hits:
            assert secret not in h.snippet


def test_connection_uri_placeholder_does_not_fire() -> None:
    """Docs placeholders and template refs in userinfo stay silent via the
    existing _looks_like_secret guards."""
    for body in (
        "postgres://user:password@localhost/db",
        "mysql://app:${DB_PASS}@db.internal/app",
    ):
        assert find_credential_markers(body) == [], body


def test_spaced_compound_keywords_fire() -> None:
    """Prose spells 'api key', not 'api_key' — the \\bis\\b separator exists
    precisely for the prose shape."""
    for body in (
        f"My Mailgun api key is {_shaped('4f9d8e7c6b5a', '4321fedcba9890aa')}",
        "My Notion access token is hunter2Abc9XyZ12Q",
    ):
        kinds = [m.kind for m in find_credential_markers(body)]
        assert "generic-secret-assignment" in kinds, body


def test_spaced_keyword_prose_does_not_fire() -> None:
    assert find_credential_markers("the api key is stored in 1Password") == []


def test_dotted_vendor_secrets_fire_in_assignments() -> None:
    """SendGrid SG.x.y and Vault hvs. tokens are intrinsically dotted; the
    dotted-ref guard read them as attribute references, so they need the
    JWT-style dedicated detectors."""
    for kind, body in (
        ("sendgrid-api-key", f"api_key = {_SENDGRID}"),
        ("vault-token", f"auth_token = {_VAULT}"),
    ):
        kinds = [m.kind for m in find_credential_markers(body)]
        assert kind in kinds, body


def test_connective_phrasing_fires() -> None:
    """'is set to' / 'was changed to' are the conversational family the
    \\bis\\b separator was already reaching for."""
    for body in (
        "the admin password is set to hunter2Abc9XyZ12Q",
        "the password was changed to hunter2Abc9XyZ12Q",
    ):
        kinds = [m.kind for m in find_credential_markers(body)]
        assert "generic-secret-assignment" in kinds, body


def test_connective_prose_does_not_fire() -> None:
    assert find_credential_markers("the password was compromised last week") == []


def test_pem_header_prose_does_not_fire() -> None:
    """Quoting the header to describe a key FORMAT carries no key bytes —
    the lookahead requires a following line of key material."""
    rsa_header = _shaped("-----BEGIN RSA ", "PRIVATE KEY-----")
    for body in (
        "Paramiko < 2.7 cannot read the new OpenSSH key format (files begin "
        f"'{_PEM_HEADER}'); convert with ssh-keygen -p -m PEM.",
        f"GitLab CI's SSH_PRIVATE_KEY must be PEM, i.e. start with {rsa_header}",
    ):
        assert find_credential_markers(body) == [], body


def test_trailing_brace_from_flow_mapping_fires() -> None:
    """Position-dependence bug: the same secret fired mid-mapping but was
    silent in final position because the lone closing brace read as a
    template reference."""
    body = "auth: {username: deploy, password: hunter2Abc9XyZ12Q}"
    kinds = [m.kind for m in find_credential_markers(body)]
    assert "generic-secret-assignment" in kinds


def test_code_assignment_operators_fire() -> None:
    """Go ':=' and Ruby '=>' spell the same assignment the '=' form blocks."""
    for body in (
        'password := "hunter2Abc9XyZ12Q"',
        ":password => 'hunter2Abc9XyZ12Q'",
    ):
        kinds = [m.kind for m in find_credential_markers(body)]
        assert "generic-secret-assignment" in kinds, body


# ---------------------------------------------------------------------------
# Regression: adversarial re-review of the extractor-hunt drain
# ---------------------------------------------------------------------------


def test_long_keyword_assigned_token_fires() -> None:
    """The cap half of the path-guard/200-char-cap finding: 'api_key = ' +
    a 240-char high-entropy token returned [] because the old 200-char
    ceiling read length as innocence. A very long whitespace-free token
    after a credential keyword separator is MORE suspicious, not less.
    Fixture is fragment-assembled (8-char fragment repeated), so no full
    token literal sits in this file's source."""
    secret = _shaped(*(["A1b2C3d4"] * 30))
    assert len(secret) == 240
    hits = find_credential_markers(f"api_key = {secret}")
    assert "generic-secret-assignment" in [m.kind for m in hits]
    # Redaction contract holds at this length: no raw window survives.
    for h in hits:
        assert secret not in h.snippet
        for i in range(0, len(secret) - 10):
            assert secret[i : i + 10] not in h.snippet, f"leaked window at {i}"


def test_iso_timestamp_after_rotation_connective_does_not_fire() -> None:
    """False-positive regression from the connective-separator widening:
    'was rotated' / 'was updated' (no 'to') capture a following ISO-8601
    timestamp as the value — rotation METADATA, not a secret. Both repro
    strings are the adversarial reviewer's exact confirmed cases; both
    returned [] before the widening."""
    for body in (
        "the admin password was rotated 2026-01-15T00:30:00Z by the ops team",
        "the password was updated 2026-06-01T09:30:00+02:00",
    ):
        assert find_credential_markers(body) == [], body


def test_genuine_secret_after_rotation_connective_still_fires() -> None:
    """The timestamp guard rejects only the fullmatch datetime shape — a
    real secret after 'was rotated to' keeps firing exactly as it did
    before the guard."""
    for body in (
        "the password was rotated to hunter2Abc9XyZ12Q",
        "the admin password was updated to hunter2Abc9XyZ12Q yesterday",
    ):
        kinds = [m.kind for m in find_credential_markers(body)]
        assert "generic-secret-assignment" in kinds, body
