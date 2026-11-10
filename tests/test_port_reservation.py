"""Two stores must reserve distinct ports before either server starts."""

from __future__ import annotations

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import errno
import socket
import sys
from threading import Barrier
from typing import Any, cast

import pytest

from bettermemory.daemon import bind


def test_two_stores_reserve_distinct_ports_before_either_server_starts() -> None:
    with closing(bind("127.0.0.1", 0)) as first:
        preferred = first.getsockname()[1]
        with closing(bind("127.0.0.1", preferred)) as second:
            assert second.getsockname()[1] != preferred
            first.listen(1)
            second.listen(1)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SO_REUSEADDR reservation")
def test_simultaneous_pre_listen_binds_retry_the_losing_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = socket.socket
    together = Barrier(2, timeout=10)
    with closing(original(socket.AF_INET, socket.SOCK_STREAM)) as held:
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", 0))
        preferred = held.getsockname()[1]

        class TogetherSocket(socket.socket):
            def bind(self, address: Any) -> None:
                super().bind(address)
                if address[1] == preferred:
                    together.wait()

        monkeypatch.setattr(socket, "socket", TogetherSocket)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(bind, "127.0.0.1", preferred) for _ in range(2)]
        try:
            listeners = [future.result(timeout=15) for future in futures]
            assert len({sock.getsockname()[1] for sock in listeners}) == 2
            assert all(
                sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                for sock in listeners
            )
        finally:
            for future in futures:
                if future.done() and future.exception() is None:
                    future.result().close()


class _Socket:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.closed = False
        self.bound: tuple[str, int] | None = None
        self.listening = False
        self.blocking: bool | None = None

    def setsockopt(self, level: int, name: int, value: int) -> None:
        pass

    def bind(self, address: tuple[str, int]) -> None:
        self.bound = address

    def listen(self) -> None:
        if self.fail:
            raise OSError(errno.EADDRINUSE, "synthetic listen collision")
        self.listening = True

    def setblocking(self, value: bool) -> None:
        self.blocking = value

    def close(self) -> None:
        self.closed = True


def _listen_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[_Socket], list[bool]]:
    created: list[_Socket] = []
    closed_before_create: list[bool] = []

    def make(*args: Any, **kwargs: Any) -> _Socket:
        closed_before_create.append(all(sock.closed for sock in created))
        found = _Socket(fail=not created)
        created.append(found)
        return found

    monkeypatch.setattr(socket, "socket", make)
    return created, closed_before_create


def test_a_listen_collision_closes_before_retrying_a_fresh_ephemeral_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, closed_before_create = _listen_collision(monkeypatch)
    answer = cast(_Socket, bind("127.0.0.1", 12345))
    try:
        assert len(created) == 2
        assert created[0].bound == ("127.0.0.1", 12345)
        assert created[0].closed
        assert created[1].bound == ("127.0.0.1", 0)
        assert answer is created[1]
        assert created[1].listening and created[1].blocking is False
        assert closed_before_create == [True, True]
    finally:
        answer.close()


def test_an_ephemeral_listen_failure_closes_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, _ = _listen_collision(monkeypatch)
    with pytest.raises(OSError, match="synthetic listen collision"):
        bind("127.0.0.1", 0)
    assert len(created) == 1 and created[0].closed
