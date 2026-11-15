"""The section builders of ``config._config_from_data``.

``_config_from_data`` reads each section's table from one parsed TOML
file and builds the ``Config``. The longer sections are built here, one
function each: it takes the section's table and the file it came from
(the user's config.toml, or the managed file or registry value) and
returns the section's settings, coerced by ``config``'s helpers with the
same defaults, labels and notices. ``config`` imports these only when
a file is read, and they read ``config``'s names through the module when
they run, so a patch on ``config`` reaches them.

The builders are apart from ``config`` so that splitting
``_config_from_data`` (ENG-97) keeps ``config`` under the package's
2,000-line bound (ENG-96).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import config as _config

if TYPE_CHECKING:
    from .config import (
        BehaviorConfig,
        CaptureConfig,
        ConfigSource,
        PolicyConfig,
        ScopesConfig,
        StorageConfig,
    )


def _storage_config(
    storage_raw: dict[str, Any], config_path: ConfigSource
) -> StorageConfig:
    """Coerce the [storage] table; takes it and the file it came from and returns the section's settings."""
    return _config.StorageConfig(
        directory=_config._coerce_optional_str(
            storage_raw.get("directory"),
            label="[storage] directory",
            config_path=config_path,
        ),
        cwd_store=_config._coerce_switch(
            storage_raw.get("cwd_store"),
            True,
            label="[storage] cwd_store",
            config_path=config_path,
        ),
    )


def _behavior_config(
    behavior_raw: dict[str, Any], config_path: ConfigSource
) -> BehaviorConfig:
    """Coerce the [behavior] table; takes it and the file it came from and
    returns the section's settings. Only the four settings a person
    decides are read; the fixed ones (`config.FIXED_SETTINGS`) keep their
    defaults."""
    return _config.BehaviorConfig(
        write_supersession=_config._coerce_bool(
            behavior_raw.get("write_supersession"), True
        ),
        prompt_recall=_config._coerce_prompt_recall(
            behavior_raw.get("prompt_recall"), config_path=config_path
        ),
        session_brief=_config._coerce_bool(behavior_raw.get("session_brief"), True),
        tombstone_retention_days=_config._coerce_int(
            behavior_raw.get("tombstone_retention_days"),
            0,
            label="[behavior] tombstone_retention_days",
            config_path=config_path,
        ),
    )


def _scopes_config(
    scopes_raw: dict[str, Any], config_path: ConfigSource
) -> ScopesConfig:
    """Coerce the [scopes] table; takes it and the file it came from and returns the section's settings."""
    return _config.ScopesConfig(
        allowed=_config._coerce_str_list(
            scopes_raw.get("allowed"),
            label="[scopes] allowed",
            config_path=config_path,
        ),
        typo_exceptions=_config._coerce_str_list(
            scopes_raw.get("typo_exceptions"),
            label="[scopes] typo_exceptions",
            config_path=config_path,
        ),
    )


def _policy_config(
    policy_raw: dict[str, Any], config_path: ConfigSource
) -> PolicyConfig:
    """Coerce the [policy] table; takes it and the file it came from and returns the section's settings."""
    return _config.PolicyConfig(
        forbid_force=_config._coerce_switch(
            policy_raw.get("forbid_force"),
            False,
            label="[policy] forbid_force",
            config_path=config_path,
        ),
        forbid_acknowledge=_config._coerce_acknowledge_names(
            policy_raw.get("forbid_acknowledge"),
            label="[policy] forbid_acknowledge",
            config_path=config_path,
        ),
    )


def _capture_config(
    capture_raw: dict[str, Any], config_path: ConfigSource
) -> CaptureConfig:
    """Coerce the [capture] table; takes it and the file it came from and returns the section's settings."""
    return _config.CaptureConfig(
        stop_checkpoint=_config._coerce_bool(capture_raw.get("stop_checkpoint"), False),
        checkpoint_tokens=_config._clamp_checkpoint_tokens(
            _config._coerce_int(
                capture_raw.get("checkpoint_tokens"),
                12_000,
                label="[capture] checkpoint_tokens",
                config_path=config_path,
            ),
            config_path=config_path,
        ),
        daily_cap=_config._coerce_int(
            capture_raw.get("daily_cap"),
            12,
            label="[capture] daily_cap",
            config_path=config_path,
        ),
    )
