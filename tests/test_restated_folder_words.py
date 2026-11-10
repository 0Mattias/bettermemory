"""Restating a question never rewrites the directory named by its data."""

from __future__ import annotations

import json
import re

import pytest

from bettermemory.membership_lines import line_restated
from bettermemory.membership_records import DUPLICATE_CHECKOUT, FIRST_SIGHT


@pytest.mark.parametrize("kind", [FIRST_SIGHT, DUPLICATE_CHECKOUT])
@pytest.mark.parametrize(
    "folder",
    ["/srv/this checkout", "/home/u/this repository/app", "/srv/plain"],
)
def test_restatement_changes_its_sentence_without_rewriting_a_folder(
    kind: str, folder: str
) -> None:
    line = line_restated("Q007", kind, "one_witness", [("app", "P001")], at=folder)
    values = [json.loads(s) for s in re.findall(r'"(?:[^"\\]|\\.)*"', line)]
    assert folder in values
    assert all(
        "that checkout" not in value and "that repository" not in value
        for value in values
    )
