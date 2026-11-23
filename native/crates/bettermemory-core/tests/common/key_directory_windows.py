"""Observe Store.open's key-directory selection on the Windows test runner."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

from bettermemory import log, store_open
from bettermemory.store import Store


def project(path):
    text = str(path)
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    units = [
        int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)
    ]
    return [list(map(ord, text)), units]


class Selected(Exception):
    """Stop after the real key-ring constructor, before it reads any key files."""


def observe(path, store_id):
    ring = log.KeyRing(path, store_id)
    raise Selected(project(ring.keys_dir))


def main():
    if sys.platform != "win32":
        raise RuntimeError("Windows Store.open is required")
    requests = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    root = Path(sys.argv[2])
    database = root / "memory.sqlite"
    keys = root / "ordinary-keys"
    if not database.exists():
        Store.create(database, keys_dir=keys).close()

    replies = []
    with patch.object(store_open, "KeyRing", observe):
        for points in requests:
            explicit = None if points is None else "".join(map(chr, points))
            try:
                with Store.open(database, read_only=True, keys_dir=explicit):
                    raise AssertionError("Store.open bypassed its key-ring constructor")
            except Selected as selected:
                replies.append(selected.args[0])
            except (OSError, RuntimeError, ValueError) as error:
                replies.append([type(error).__name__, list(map(ord, str(error)))])

    with Store.open(database, read_only=True, keys_dir=keys) as store:
        if not store.has_key or store.keys_dir != keys:
            raise AssertionError(
                "the signed control store did not recognize its own key"
            )
    print(json.dumps([sys.version_info[:2], replies], ensure_ascii=True))


def hresults(statuses):
    from ctypes import HRESULT

    replies = []
    for status in statuses:
        try:
            HRESULT._check_retval_(status)
        except OSError as error:
            replies.append(
                [type(error).__name__, error.winerror, error.errno, str(error)]
            )
        else:
            replies.append(None)
    print(json.dumps(replies, ensure_ascii=True))


if __name__ == "__main__":
    if sys.argv[1] == "hresults":
        hresults(json.loads(sys.argv[2]))
    else:
        main()
