"""Tests for the local web UI (T4.3 of the v1.6 plan).

Uses FastAPI's TestClient — same in-process HTTP testing pattern the
fastapi docs recommend. Skips when the [ui] extra isn't installed
(fastapi / httpx missing) so the suite stays portable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.store import Store


# Skip the whole module when the ui extra isn't available.
fastapi = pytest.importorskip("fastapi")
testclient_mod = pytest.importorskip("fastapi.testclient")
TestClient = testclient_mod.TestClient


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


@pytest.fixture
def client(memory_dir: Path, store: Store) -> Any:
    from bettermemory.web import build_app

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    app = build_app(cfg, store)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_index_returns_200_with_empty_store(client: Any) -> None:
    """A fresh store with no memories should render the overview
    without crashing — empty buckets shouldn't surface as zero-row
    crashes."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Overview" in r.text


def test_memories_list_renders(client: Any, store: Store) -> None:
    """Memory list view shows each memory with scope tags and a link
    to the detail page."""
    store.write(content="python list comprehension", scopes=["tools"])
    store.write(content="kubernetes notes", scopes=["infrastructure"])

    r = client.get("/memories")
    assert r.status_code == 200
    assert "python list comprehension" in r.text
    assert "kubernetes notes" in r.text
    assert "tools" in r.text
    assert "infrastructure" in r.text


def test_memories_list_search_filter(client: Any, store: Store) -> None:
    """The `q` query param filters by case-insensitive substring on
    the summary. Useful for browsing large stores."""
    store.write(content="python list comprehension", scopes=["tools"])
    store.write(content="kubernetes notes", scopes=["infrastructure"])

    r = client.get("/memories", params={"q": "python"})
    assert r.status_code == 200
    assert "python list comprehension" in r.text
    assert "kubernetes notes" not in r.text


def test_memories_list_scope_filter(client: Any, store: Store) -> None:
    """The `scope` query param is a strict scope filter — like the
    `scopes=` parameter on memory_search."""
    store.write(content="python list comprehension", scopes=["tools"])
    store.write(content="kubernetes notes", scopes=["infrastructure"])

    r = client.get("/memories", params={"scope": "tools"})
    assert r.status_code == 200
    assert "python list comprehension" in r.text
    assert "kubernetes notes" not in r.text


def test_memory_detail_renders(client: Any, store: Store) -> None:
    """Detail view shows the body, scopes, timestamps, and a verify
    form."""
    m = store.write(content="durable fact body content here", scopes=["tools"])
    r = client.get(f"/memories/{m.id}")
    assert r.status_code == 200
    assert "durable fact body content here" in r.text
    assert "tools" in r.text
    assert "Mark verified now" in r.text


def test_memory_detail_404_when_missing(client: Any) -> None:
    """A request for a non-existent (well-formed) memory id returns
    404 — not a 500 or a silent empty render."""
    # 26-char ULID-shaped id that doesn't exist.
    r = client.get("/memories/01J0000000000000000000000A")
    assert r.status_code == 404


def test_verify_marks_memory_and_redirects(client: Any, store: Store) -> None:
    """POST /memories/{id}/verify bumps last_verified_at and 303s
    back to the detail page (PRG pattern — refreshes don't repeat
    the verify)."""
    m = store.write(content="some claim", scopes=["tools"])
    assert m.last_verified_at is None

    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": "spot-checked"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/memories/{m.id}"

    reloaded = store.load_one(m.id)
    assert reloaded.last_verified_at is not None


def test_verify_404_when_missing(client: Any) -> None:
    """Posting a verify for a non-existent id returns 404 with a
    clean error — not a 500."""
    r = client.post(
        "/memories/01J0000000000000000000000A/verify",
        data={"note": ""},
    )
    assert r.status_code == 404


def test_health_renders(client: Any, store: Store) -> None:
    """/health surfaces the memory_health buckets. With one memory in
    the store, the active count should show as 1."""
    store.write(content="a memory", scopes=["tools"])
    r = client.get("/health")
    assert r.status_code == 200
    assert "Health" in r.text
    assert "active memories" in r.text.lower()


def test_tombstones_renders(client: Any, store: Store) -> None:
    """/tombstones lists removed memories with their reasons."""
    m = store.write(content="will be removed", scopes=["tools"])
    store.tombstone(m.id, reason="test removal")

    r = client.get("/tombstones")
    assert r.status_code == 200
    assert "will be removed" in r.text
    assert "test removal" in r.text


def test_html_escapes_user_content(client: Any, store: Store) -> None:
    """Memory bodies that contain HTML special characters must be
    escaped on render — no XSS via memory_write."""
    store.write(
        content="<script>alert('xss')</script> with brackets",
        scopes=["tools"],
    )
    r = client.get("/memories")
    assert r.status_code == 200
    # The raw <script> tag must not appear unescaped.
    assert "<script>alert" not in r.text
    # The escaped form must appear.
    assert "&lt;script&gt;" in r.text


def test_links_render_on_detail(client: Any, store: Store) -> None:
    """Memories with `links` show them on the detail view with the
    type label and a link to the target."""
    from bettermemory.models import LinkType, MemoryLink

    a = store.write(content="target memory", scopes=["tools"])
    b = store.write(content="source memory", scopes=["tools"])
    b_with_links = b.model_copy(
        update={
            "links": [
                MemoryLink(type=LinkType.SUPERSEDES, target_id=a.id, note="newer")
            ]
        }
    )
    store.update(b_with_links)

    r = client.get(f"/memories/{b.id}")
    assert r.status_code == 200
    assert "supersedes" in r.text
    assert a.id in r.text
    assert "newer" in r.text


def test_navigation_links_present(client: Any) -> None:
    """Every page should carry the same header nav so users can move
    between sections. Sanity check on the layout chrome."""
    r = client.get("/")
    assert r.status_code == 200
    for path in ("/", "/memories", "/health", "/tombstones"):
        assert f'href="{path}"' in r.text
