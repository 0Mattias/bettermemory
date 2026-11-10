"""Claim 9: join_project with an id that names no project must refuse with
'no project <id>' (its own guard, and unjoin_project's refusal), not die
with an uncaught KeyError."""

from __future__ import annotations

import pytest

from .test_membership_questions import Bench, bench

__all__ = ["bench"]


@pytest.mark.parametrize("pid", ["P999", "global"])
async def test_join_project_refuses_an_id_that_names_no_project(
    bench: Bench, pid: str
) -> None:
    before = len(bench.rows("project_absorbed", "project_merged"))
    # hookless client: no words kept for the checkout, the quote is taken as given
    with pytest.raises(Exception) as exc:
        await bench.admin(action="join_project", id=pid, said="merge it please")
    assert f"no project {pid}" in str(exc.value), (
        f"expected the 'no project {pid}' refusal, got "
        f"{type(exc.value).__name__}: {exc.value!r} (cause {exc.value.__cause__!r})"
    )
    assert len(bench.rows("project_absorbed", "project_merged")) == before


async def test_unjoin_sibling_refuses_cleanly(bench: Bench) -> None:
    with pytest.raises(Exception, match="no project P999"):
        await bench.admin(action="unjoin_project", id="P999")
