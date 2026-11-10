"""The HTTP server uses Uvicorn's supported libuv loop on Windows.

The Windows lifecycle regression exercises a real server with aborted
connections and more idle sockets than SelectSelector supports.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType

import pytest
import uvicorn  # noqa: F401 (load before the simulated platform is set)
from starlette.applications import Starlette

from bettermemory import daemon


def test_windows_server_loads_the_winloop_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured custom factory is loaded by Uvicorn, rather than
    its default Proactor factory; no global asyncio policy is changed.
    """
    loop = asyncio.SelectorEventLoop()
    module = ModuleType("winloop")

    def new_event_loop() -> asyncio.AbstractEventLoop:
        return loop

    def changed_policy(*args: object, **kwargs: object) -> None:
        raise AssertionError("the server changed the process-wide loop policy")

    monkeypatch.setattr(asyncio, "set_event_loop_policy", changed_policy)
    monkeypatch.setattr(module, "new_event_loop", new_event_loop, raising=False)
    monkeypatch.setitem(sys.modules, "winloop", module)
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        config = daemon._uvicorn_config(Starlette(), "127.0.0.1", 12345)
        factory = config.get_loop_factory()
        assert factory is not None
        assert factory() is loop
        assert config.host == "127.0.0.1" and config.port == 12345
        assert config.timeout_graceful_shutdown == daemon.GRACEFUL_SHUTDOWN_SECONDS
    finally:
        loop.close()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_keep_uvicorn_auto(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    config = daemon._uvicorn_config(Starlette(), "127.0.0.1", 12345)
    assert config.loop == "auto"
