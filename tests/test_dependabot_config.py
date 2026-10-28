"""`.github/dependabot.yml`: Python dependencies go through the `uv`
ecosystem, which reads and writes uv.lock, and never widen a range.

The `pip` updater the file carried before 9.0.0rc12 never opened a pull
request: it does not maintain uv.lock, the file every CI job installs from.
`tests/test_commit_lint.py` separately grades each updater's commit prefix
against the commit linter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

CONFIG = Path(__file__).resolve().parents[1] / ".github" / "dependabot.yml"


@pytest.fixture(scope="module")
def updaters() -> dict[str, dict[str, Any]]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert config["version"] == 2
    found: dict[str, dict[str, Any]] = {}
    for updater in config["updates"]:
        ecosystem = updater["package-ecosystem"]
        assert ecosystem not in found, f"two {ecosystem} updaters"
        found[ecosystem] = updater
    return found


def test_python_dependencies_go_through_uv_and_not_pip(
    updaters: dict[str, dict[str, Any]],
) -> None:
    assert "pip" not in updaters
    assert updaters["uv"]["directory"] == "/"


def test_the_uv_updater_touches_only_the_lock(
    updaters: dict[str, dict[str, Any]],
) -> None:
    """lockfile-only: the ranges in pyproject.toml carry their reasons in
    comments there (the mcp `<3.0.0` cap is an incident's lesson), and only
    a person widens one."""
    assert updaters["uv"]["versioning-strategy"] == "lockfile-only"


def test_the_uv_updater_waits_a_week_for_a_new_release(
    updaters: dict[str, dict[str, Any]],
) -> None:
    assert updaters["uv"]["cooldown"] == {"default-days": 7}


def test_the_actions_updater_is_kept(updaters: dict[str, dict[str, Any]]) -> None:
    assert updaters["github-actions"]["directory"] == "/"
    assert set(updaters) == {"github-actions", "uv"}


def test_every_updater_runs_weekly_with_a_pinned_prefix(
    updaters: dict[str, dict[str, Any]],
) -> None:
    for ecosystem, updater in updaters.items():
        assert updater["schedule"]["interval"] == "weekly", ecosystem
        assert updater["commit-message"]["prefix"], ecosystem


def test_minor_and_patch_updates_arrive_grouped(
    updaters: dict[str, dict[str, Any]],
) -> None:
    """Every group takes minor and patch updates only, so a major update
    inside a range comes as a pull request of its own."""
    for ecosystem, updater in updaters.items():
        groups = updater["groups"]
        assert groups, ecosystem
        for name, group in groups.items():
            assert group["update-types"] == ["minor", "patch"], f"{ecosystem}/{name}"
    assert set(updaters["uv"]["groups"]) == {"python"}
