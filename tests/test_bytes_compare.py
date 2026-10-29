"""Every constant-time comparison of a value read from input compares bytes.

`hmac.compare_digest` and `secrets.compare_digest` raise TypeError on a
str that is not ASCII. A digest or MAC read off a store file, a bundle's
manifest, an anchor or a state file holds whatever an editor of that file
wrote, so each comparison encodes both sides first (`log.same_mac`) and a
non-ASCII value reads as a mismatch, never as a traceback. The manifest's
and the anchor file's readers hold their digests to hex before anything
compares them; these tests reach each comparison past that check, the way
a caller of the public function, or a later reader that forgot the check,
would.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import bettermemory
from bettermemory import (
    _daemon_client,
    anchor,
    audit_export,
    backup,
    history,
    log,
    store_provenance,
)
from bettermemory.anchor import (
    REASON_DIGEST,
    STATUS_MISMATCH,
    Anchor,
    check_anchors,
    take,
)
from bettermemory.backup import SNAPSHOT_NAME, create_backup, verify_bundle
from bettermemory.store import STORE_FILENAME, Store

_NOT_ASCII = "é" * 64


def test_an_anchor_with_a_non_ascii_chain_digest_reads_as_a_mismatch(
    store: Store,
) -> None:
    store.write(content="anchored", scopes=["x"])
    anchor = take(store)
    edited = Anchor(
        store_id=anchor.store_id,
        seq=anchor.seq,
        mac=anchor.mac,
        chain_sha256=_NOT_ASCII,
        created_at=anchor.created_at,
    )
    [result] = check_anchors(store.conn, store.store_id, [("kept.json", edited)])
    assert result["status"] == STATUS_MISMATCH
    assert result["reason"] == REASON_DIGEST


def test_a_manifest_digest_and_mac_that_are_not_ascii_read_as_mismatched(
    tmp_path: Path, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the manifest's shape check, a file hash and the manifest's MAC
    that are not ASCII are a mismatch each, and the verdict is
    `mismatched`, not a TypeError out of `backup verify`."""
    path = tmp_path / "store" / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir):
        pass
    bundle = Path(create_backup(path, out_dir=tmp_path / "bundles")["bundle"])
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][SNAPSHOT_NAME]["sha256"] = _NOT_ASCII
    manifest["mac"] = _NOT_ASCII
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(backup, "_hex_digest", lambda value: isinstance(value, str))

    report = verify_bundle(bundle)

    problems = {str(p["problem"]) for p in report["problems"]}
    assert report["status"] == "mismatched"
    assert "sha256_mismatch" in problems
    assert "manifest_mac_mismatch" in problems


def test_a_manifest_field_utf8_cannot_encode_reads_as_a_mac_mismatch(
    tmp_path: Path, keys_dir: Path
) -> None:
    """The manifest MAC is computed over the manifest as read, and JSON
    spells a lone surrogate (``"\\ud800"``) in any field the shape check
    does not hold to hex: an edited ``created_at`` or an added field. The
    comparison must see a MAC that does not match, and `backup verify`
    report it, rather than end in UnicodeEncodeError before comparing."""
    path = tmp_path / "store" / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir):
        pass
    bundle = Path(create_backup(path, out_dir=tmp_path / "bundles")["bundle"])
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["note"] = "\ud800"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_bundle(bundle)

    assert report["manifest_mac"] == "mismatch"
    assert report["status"] == "mismatched"
    assert {"problem": "manifest_mac_mismatch"} in report["problems"]


def test_a_manifest_mac_is_unchanged_for_text_utf8_encodes(keys_dir: Path) -> None:
    """``surrogatepass`` changes nothing for a string UTF-8 encodes, so a
    bundle signed before verifies the same: the MAC over a manifest with
    non-ASCII text is the one plain UTF-8 gives, and one holding a lone
    surrogate (a POSIX store path that is not UTF-8) has a MAC at all."""
    import hashlib
    import hmac

    from bettermemory.log import canonical_payload

    key = b"k" * 32
    body = {"source": "/home/é/store"}
    plain = hmac.new(
        key,
        backup._MANIFEST_MAC_CONTEXT + canonical_payload(body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert backup.manifest_mac(key, {**body, "mac": "x"}) == plain
    escaped = backup.manifest_mac(key, {"source": "/home/\udce9/store"})
    assert len(escaped) == 64


def test_a_state_file_token_utf8_cannot_encode_is_not_ours() -> None:
    """JSON spells a lone surrogate that UTF-8 refuses to encode: a state
    file holding one gives a proof no daemon answers with, so the reply
    reads as not ours rather than a UnicodeEncodeError out of the hook."""
    raw = json.loads(
        '{"pid": 1, "port": 1, "store": "s", "token": "\\ud800", "version": "9", '
        f'"{_daemon_client.PROOF_STATE_KEY}": 1}}'
    )
    nonce = "ab" * 16
    assert not _daemon_client.proves(raw, {"proof": "0" * 64}, nonce)
    # The daemon side computes the same proof for the same token, so a
    # daemon that really holds such a token still proves itself.
    proof = _daemon_client.health_proof(raw["token"], nonce)
    assert _daemon_client.proves(raw, {"proof": proof}, nonce)


def test_the_package_has_one_mac_comparison() -> None:
    """`audit_export` kept a copy of `log.same_mac` (ENG-93 item 12). One
    definition is left, in log.py, and every module that names
    `same_mac` holds that one, `audit_export` and `anchor` included."""
    src = Path(bettermemory.__file__).parent
    defined = [
        str(path.relative_to(src))
        for path in sorted(src.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "same_mac"
    ]
    assert defined == ["log.py"]
    for module in (anchor, audit_export, backup, history, store_provenance):
        assert module.same_mac is log.same_mac, module.__name__
    # The kept comparison is the one the export and the anchor check need:
    # a non-ASCII column reads as a mismatch, not a TypeError.
    assert log.same_mac(_NOT_ASCII, "0" * 64) is False
    assert log.same_mac("ab" * 32, "ab" * 32) is True
