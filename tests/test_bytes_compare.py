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

import json
from pathlib import Path

import pytest

from bettermemory import _daemon_client, backup
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
