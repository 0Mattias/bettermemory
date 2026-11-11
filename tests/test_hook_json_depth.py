"""ENG-279: a JSON decoder depth refusal keeps the hook's reply shape."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.daemon import MAX_BODY_BYTES, build_app
from bettermemory.store import Store

from .test_daemon import BASE, TOKEN, _auth


@pytest.fixture
def hook_store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path / "store")
    try:
        yield store
    finally:
        store.close()


def _payload(kind: str) -> bytes:
    if kind == "deep_array":
        depth = sys.getrecursionlimit() + 100
        return b"[" * depth + b"0" + b"]" * depth
    if kind == "deep_object":
        return b'{"ignored":' + _payload("deep_array") + b"}"
    return {"malformed": b"{", "non_object": b"[]", "empty": b"{}"}[kind]


@pytest.mark.parametrize(
    ("event", "surface"),
    [
        ("session-start", "session_brief"),
        ("prompt", "prompt_recall"),
        ("stop", "turn_audit"),
    ],
)
@pytest.mark.parametrize(
    "kind", ["deep_array", "deep_object", "malformed", "non_object", "empty"]
)
async def test_a_refused_json_body_keeps_the_hook_reply_shape(
    hook_store: Store, tmp_path: Path, event: str, surface: str, kind: str
) -> None:
    payload = _payload(kind)
    assert len(payload) < MAX_BODY_BYTES
    config = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    app = build_app(config=config, store=hook_store, token=TOKEN, shutdown=lambda: None)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as client:
            response = await client.post(
                f"/api/v1/hook/{event}",
                content=payload,
                headers=_auth(**{"Content-Type": "application/json"}),
            )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["surface"] == surface
    assert {"stdout", "ids", "chars", "rule", "event_id"} <= body.keys()
    assert body["stdout"] == "" and body["ids"] == [] and body["chars"] == 0
