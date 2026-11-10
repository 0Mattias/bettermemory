"""Exact native membership values survive the UTF-8 store boundary."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bettermemory.membership import INDETERMINATE
from bettermemory.store import Store

from .test_membership_model import World, world

__all__ = ["world"]

RAW = "/w/bytes-" + chr(0xDCFF)
PRINTED = "/w/bytes-\\udcff"


def _view(path: str, order: int = 1) -> dict[str, object]:
    return {
        "checkout": "K001",
        "machine": "M001",
        "path": path,
        "first_seen": "2026-01-01",
        "last_seen": "2026-01-02",
        "seen_order": order,
    }


def test_native_view_keys_do_not_alias_their_printable_spelling(tmp_path: Path) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        store.put_membership("checkout_view", _view(RAW, 2))
        store.put_membership("checkout_view", _view(PRINTED, 1))
        assert store.membership_rows("checkout_views", where={"path": RAW}) == [
            _view(RAW, 2)
        ]
        assert store.membership_rows("checkout_views", where={"path": PRINTED}) == [
            _view(PRINTED, 1)
        ]
        rows = store.membership_rows(
            "checkout_views",
            column_in=("path", [RAW, PRINTED]),
            order_by=("seen_order",),
        )
        assert [r["path"] for r in rows] == [PRINTED, RAW]
        assert store.next_membership_number("checkout_views", "seen_order") == 3
        assert store.log_verify()["status"] == "ok"
        assert store.refold()["status"] == "ok"
        store.delete_membership(
            "checkout_view", {"checkout": "K001", "machine": "M001", "path": RAW}
        )
        assert store.membership_rows("checkout_views") == [_view(PRINTED, 1)]
        assert store.refold()["status"] == "ok"


def test_native_rows_survive_reopen_and_refold(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    keys = tmp_path / "keys"
    with Store.create(path, keys_dir=keys) as store:
        store.put_membership("checkout_view", _view(RAW))
        store.record_membership(
            "checkout_view_added", {"checkout": "K001", "path": RAW}
        )
    with Store.open(path, keys_dir=keys, allow_rekey=True) as store:
        assert store.membership_rows("checkout_views") == [_view(RAW)]
        assert store.log_verify()["status"] == "ok"
        assert store.refold()["status"] == "ok"


@pytest.mark.parametrize("field", ["cwd", "root", "git_dir"])
def test_membership_resolves_native_strings_without_refusal(
    world: World, field: str
) -> None:
    repo = world.repo("/w/app")
    values: dict[str, Any] = {field: RAW}
    loc = replace(repo.location(), **values)
    res = world.model.resolve(loc)
    assert res.outcome != INDETERMINATE
    assert res.project is not None and res.checkout is not None
    checkout = world.model.checkout(res.checkout)
    if field == "git_dir":
        assert checkout.git_dir == RAW
    if field == "root":
        assert checkout.views[-1].path == RAW
    assert world.store.log_verify()["status"] == "ok"
    assert world.store.refold()["status"] == "ok"


def test_native_key_deletions_are_idempotent(tmp_path: Path) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        store.put_membership("checkout_view", _view(RAW))
        key = {"checkout": "K001", "machine": "M001", "path": RAW}
        for _ in range(2):
            store.delete_membership("checkout_view", key)
        assert store.membership_rows("checkout_views") == []
        assert store.refold()["status"] == "ok"


@pytest.mark.parametrize(
    "text", ["{", "[]", '{"checkout":"K001","machine":"M001","path":null}']
)
def test_a_malformed_native_row_is_not_a_membership_answer(
    tmp_path: Path, text: str
) -> None:
    with Store.create(tmp_path / "store.sqlite") as store:
        store.put_membership("checkout_view", _view(RAW))
        store.conn.execute("UPDATE native_membership_rows SET row_text = ?", (text,))
        assert store.membership_rows("checkout_views") == []
        assert store.log_verify()["status"] == "tampered"
