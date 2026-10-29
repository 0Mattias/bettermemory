"""verify's anchoring and the membership model read "below a recorded view"
by one rule.

`verify.Anchoring.translate` asks whether a path lies under a recorded
view before it hands the path to the model to translate. Its own test
checked "/" and the platform's separator while the model's checks either
slash, so on POSIX a path recorded on Windows below a backslash view was
never translated: it was stat'd as spelled and an attested one read as a
claim-anchored miss (ENG-166). Both read `_paths.inside` now. The model
end of the same read, on a real store, is
`test_membership_model.test_an_attestation_recorded_on_another_kind_of_machine_reads_here`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from bettermemory import membership_records
from bettermemory.verify import Anchoring

#: (path, root, inside), each read the same on every host.
TABLE = [
    ("C:\\Users\\me\\proj\\src\\x.py", "C:\\Users\\me\\proj", True),
    ("C:\\Users\\me\\proj", "C:\\Users\\me\\proj\\", True),
    ("C:\\Users\\me\\proj2\\x.py", "C:\\Users\\me\\proj", False),
    ("D:\\Users\\me\\proj\\x.py", "C:\\Users\\me\\proj", False),
    ("/home/me/proj/src/x.py", "/home/me/proj", True),
    ("/home/me/proj", "/home/me/proj/", True),
    ("/home/me/project/x.py", "/home/me/proj", False),
    ("/home/me/proj/x.py", "C:\\Users\\me\\proj", False),
    # Mixed separators in one spelling do not meet the uniform twin.
    ("C:\\Users\\me/proj/x.py", "C:\\Users\\me\\proj", False),
]


@pytest.mark.parametrize(("path", "root", "expected"), TABLE)
def test_inside_reads_either_slash_on_every_host(
    path: str, root: str, expected: bool
) -> None:
    from bettermemory import _paths

    assert _paths.inside(path, root) is expected
    assert membership_records._inside(path, root) is expected


class _Model:
    """The two calls `Anchoring.translate` makes of the model: the recorded
    views, and the translation of a path below one of them."""

    def __init__(self, view: str) -> None:
        self.view = view
        self.asked: list[str] = []

    def checkouts(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(views=[SimpleNamespace(path=self.view)])]

    def translate(self, path: str, caller: object) -> tuple[str, str]:
        self.asked.append(path)
        return "/live/src/x.py", "checkout"


@pytest.mark.parametrize(
    ("view", "path"),
    [
        ("C:\\w\\app", "C:\\w\\app\\src\\x.py"),
        ("C:\\w\\app\\", "C:\\w\\app\\src\\x.py"),
        ("/w/app", "/w/app/src/x.py"),
    ],
)
def test_translate_hands_a_path_below_a_view_to_the_model(view: str, path: str) -> None:
    model = _Model(view)
    anchoring = Anchoring(cast(Any, model), None)
    assert anchoring.translate(path) == ("/live/src/x.py", "checkout")
    assert model.asked == [path]


def test_translate_reads_a_path_under_no_view_as_spelled() -> None:
    model = _Model("C:\\w\\app")
    anchoring = Anchoring(cast(Any, model), None)
    assert anchoring.translate("C:\\w\\apple\\x.py") == (
        "C:\\w\\apple\\x.py",
        "absolute",
    )
    assert model.asked == []
