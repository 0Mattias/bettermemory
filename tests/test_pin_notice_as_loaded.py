"""The pinned-key notice compares a user's line with the pin as the
loader would load it, not as written (ENG-93 item 5).

Three contracts, each pinned with the behaviour it promises:

1. **As loaded, not as written.** A user line for a pinned key restates
   the pin only when the loader, reading the line alone, takes it as
   written and loads the pinned value. Python's ``1 == True`` no longer
   hides ``strict_provenance = 1`` under a pinned ``true`` (the loader
   reads that line as the default ``false``) or ``forbid_force = 1`` (the
   loader refuses it), and ``"true"`` under a pinned ``true`` (which loads
   as the pin) no longer draws the notice. A line the loader reads only
   through a fallback or a clamp draws it even where the fallback lands
   on the pinned value.
2. **The read is silent.** The fallback and clamp warnings the loader
   would log for the line are not logged: the line is dropped unread, and
   the notice is its one word.
3. **The read hides nothing else.** Outside the read, and in another
   thread during it, every warning is logged as before; and the verdict
   does not depend on the logging configuration.
4. **`config check` and `config show` agree with the loader.** A pinned
   line is a `pinned` note, and its value is named in `show`, unless it
   reads as the pin by the same comparison.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from bettermemory import config as _config
from bettermemory.cli import config_cmd
from bettermemory.config import load_config


def _user_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _pin_notices(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "pins" in r.getMessage()]


@pytest.mark.parametrize(
    ("managed", "user", "notice"),
    [
        # `1 == True`, but a boolean reads the integer 1 as its default,
        # false here: the line chooses another value than the pin.
        (
            "[security]\nstrict_provenance = true\n",
            "[security]\nstrict_provenance = 1\n",
            True,
        ),
        # The same fallback landing on the pinned value (the default is
        # true): the line is still not read as written.
        ("[telemetry]\nenabled = true\n", "[telemetry]\nenabled = 1\n", True),
        # A switch refuses an integer outright.
        ("[storage]\ncwd_store = false\n", "[storage]\ncwd_store = 0\n", True),
        ("[policy]\nforbid_force = true\n", "[policy]\nforbid_force = 1\n", True),
        # A string the loader reads as the pinned boolean restates it.
        (
            "[security]\nstrict_provenance = true\n",
            '[security]\nstrict_provenance = "true"\n',
            False,
        ),
        ("[storage]\ncwd_store = false\n", '[storage]\ncwd_store = "off"\n', False),
        # A string the loader reads as the other boolean is a choice.
        (
            "[security]\nstrict_provenance = true\n",
            '[security]\nstrict_provenance = "false"\n',
            True,
        ),
        # A string the loader does not recognise falls back, to the
        # default (false) or to a default equal to the pin (true).
        (
            "[security]\nstrict_provenance = true\n",
            '[security]\nstrict_provenance = "ture"\n',
            True,
        ),
        ("[telemetry]\nenabled = true\n", '[telemetry]\nenabled = "ture"\n', True),
        # Spellings the loader normalises to the pinned value.
        (
            '[behavior]\nsearch_mode = "bm25"\n',
            '[behavior]\nsearch_mode = "BM25"\n',
            False,
        ),
        ("[daemon]\nport = 7400\n", '[daemon]\nport = "7400"\n', False),
        # A clamp that lands on the pinned value is not the line as written.
        (
            "[capture]\ncheckpoint_tokens = 500\n",
            "[capture]\ncheckpoint_tokens = 100\n",
            True,
        ),
        # A value the loader cannot convert at all (OverflowError, not
        # ValueError) is refused, and the load still succeeds.
        ("[daemon]\nport = 7400\n", "[daemon]\nport = inf\n", True),
        # The legacy prompt_recall boolean as before: `false` reads "off".
        (
            '[behavior]\nprompt_recall = "off"\n',
            "[behavior]\nprompt_recall = false\n",
            False,
        ),
        # Lossy numeric coercions are not the line as written: a float the
        # integer truncates, and a boolean read as a number.
        ("[daemon]\nport = 7400\n", "[daemon]\nport = 7400.9\n", True),
        ("[daemon]\nport = 7400\n", "[daemon]\nport = 7400.0\n", False),
        (
            "[behavior]\ndefault_max_results = 1\n",
            "[behavior]\ndefault_max_results = true\n",
            True,
        ),
        (
            "[behavior]\nrecency_boost_half_life_days = 1.0\n",
            "[behavior]\nrecency_boost_half_life_days = true\n",
            True,
        ),
    ],
)
def test_a_pinned_line_is_compared_with_the_pin_as_the_loader_reads_it(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    managed: str,
    user: str,
    notice: bool,
) -> None:
    managed_config_path.write_text(managed, "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(_user_config(tmp_path, user))
    notices = _pin_notices(caplog)
    assert bool(notices) is notice, notices
    # Whatever the line says, the pin is in force: the same values as
    # under a config.toml that sets nothing.
    empty = tmp_path / "empty"
    empty.mkdir()
    pinned = load_config(_user_config(empty, ""))
    assert loaded.managed_keys == pinned.managed_keys
    for name in loaded.managed_keys:
        section, key = name.split(".")
        assert getattr(getattr(loaded, section), key) == getattr(
            getattr(pinned, section), key
        )


def test_one_under_a_pinned_true_names_the_line_and_both_files(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The case the review raised, end to end: the notice names the key
    and the managed file, and the pinned value is in force."""
    managed_config_path.write_text("[security]\nstrict_provenance = true\n", "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        loaded = load_config(
            _user_config(tmp_path, "[security]\nstrict_provenance = 1\n")
        )
    assert loaded.security.strict_provenance is True
    notices = _pin_notices(caplog)
    assert len(notices) == 1, notices
    assert "[security] `strict_provenance`" in notices[0]
    assert str(managed_config_path) in notices[0]


@pytest.mark.parametrize(
    ("managed", "user", "silenced"),
    [
        (
            '[behavior]\nsearch_mode = "bm25"\n',
            '[behavior]\nsearch_mode = "Semantic"\n',
            "falling back",
        ),
        (
            "[capture]\ncheckpoint_tokens = 500\n",
            "[capture]\ncheckpoint_tokens = 100\n",
            "is below",
        ),
        (
            '[behavior]\nprompt_recall = "off"\n',
            '[behavior]\nprompt_recall = "sometimes"\n',
            "falling back",
        ),
    ],
)
def test_the_read_of_a_pinned_line_logs_only_the_notice(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
    managed: str,
    user: str,
    silenced: str,
) -> None:
    """The loader would warn about each of these lines; pinned, the line
    is dropped unread and the notice is the one record about it."""
    managed_config_path.write_text(managed, "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        load_config(_user_config(tmp_path, user))
    messages = [r.getMessage() for r in caplog.records]
    assert len(_pin_notices(caplog)) == 1, messages
    assert not [m for m in messages if silenced in m], messages
    assert _config._TRIAL.get() is None


def test_outside_the_read_every_warning_is_logged_as_before(
    tmp_path: Path,
    managed_config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pinned line's read installs the filter on the logger; a fallback
    on a line nothing pins is still logged after it."""
    managed_config_path.write_text("[daemon]\nport = 7400\n", "utf-8")
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        load_config(
            _user_config(
                tmp_path,
                '[daemon]\nport = "7400"\n[behavior]\nsearch_mode = "Semantic"\n',
            )
        )
    messages = [r.getMessage() for r in caplog.records]
    assert [m for m in messages if "falling back" in m], messages
    assert _pin_notices(caplog) == []


def test_a_read_in_one_thread_hides_nothing_another_thread_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The read's state is a ContextVar: while one thread holds a read
    open, a warning another thread logs reaches the log."""
    logger = logging.getLogger("bettermemory.config")
    logger.addFilter(_config._trial_filter)
    go = threading.Event()

    def other() -> None:
        go.wait(5)
        logger.warning("logged from another thread")

    thread = threading.Thread(target=other)
    thread.start()
    notes: list[str] = []
    with caplog.at_level(logging.WARNING, logger="bettermemory.config"):
        token = _config._TRIAL.set(notes)
        try:
            logger.warning("logged inside the read")
            go.set()
            thread.join(5)
        finally:
            _config._TRIAL.reset(token)
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["logged from another thread"]
    assert notes == ["logged inside the read"]


@pytest.fixture(params=["logging.disable", "logger.disabled"])
def warnings_off(request: pytest.FixtureRequest) -> Iterator[None]:
    """Warnings switched off before any filter can see them: a level
    above WARNING for the process, or the logger disabled (what
    dictConfig's disable_existing_loggers does)."""
    logger = logging.getLogger("bettermemory.config")
    was_disabled = logger.disabled
    if request.param == "logging.disable":
        logging.disable(logging.WARNING)
    else:
        logger.disabled = True
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)
        logger.disabled = was_disabled


@pytest.mark.parametrize(
    ("section", "key", "raw", "pinned"),
    [
        ("behavior", "search_mode", "Semantic", "hybrid"),
        ("behavior", "prompt_recall", "sometimes", "project"),
        ("capture", "checkpoint_tokens", 100, 500),
    ],
)
def test_the_verdict_does_not_depend_on_the_logging_configuration(
    tmp_path: Path,
    warnings_off: None,
    section: str,
    key: str,
    raw: object,
    pinned: object,
) -> None:
    """Each line falls back, or is clamped, onto the pinned value. With
    warnings off no filter sees the warning, and the coercion's own note
    still says the line was not read as written."""
    assert not _config._reads_as_pin(
        section, key, raw, config_path=tmp_path / "config.toml", pinned=pinned
    )


@pytest.mark.parametrize(
    ("user", "named"),
    [
        ("strict_provenance = 1", True),
        ('strict_provenance = "true"', False),
        ('strict_provenance = "false"', True),
        ("strict_provenance = true", False),
    ],
)
def test_config_check_and_show_compare_a_pinned_line_as_the_loader_does(
    managed_config_path: Path, user: str, named: bool
) -> None:
    """`config check` notes a pinned line, and `config show` names its
    value, exactly when the loader's notice would not take it for the
    pin: `1` under a pinned `true` is named, `"true"` is not."""
    managed_config_path.write_text("[security]\nstrict_provenance = true\n", "utf-8")
    _config.default_config_path().write_text(f"[security]\n{user}\n", encoding="utf-8")
    report = config_cmd.check_report()
    pinned = [f for f in report["findings"] if f["code"] == "pinned"]
    assert bool(pinned) is named, pinned
    assert report["exit_code"] == 0
    entry = config_cmd.show_report()["settings"]["security"]["strict_provenance"]
    assert entry["value"] is True
    assert entry["source"] == "managed"
    assert ("user_value" in entry) is named, entry
