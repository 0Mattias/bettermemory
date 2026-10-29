"""The section builders of ``config._config_from_data``.

``_config_from_data`` reads each section's table from one parsed TOML
file and builds the ``Config``. The longer sections are built here, one
function each: it takes the section's table and the file it came from
(the user's config.toml, or the managed file or registry value) and
returns the section's settings, coerced by ``config``'s helpers with the
same defaults, labels and notices. ``config`` imports these only when
a file is read, and they read ``config``'s names through the module when
they run, so a patch on ``config`` reaches them.
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
    """Coerce the [behavior] table; takes it and the file it came from and returns the section's settings."""
    return _config.BehaviorConfig(
        default_max_results=_config._coerce_int(
            behavior_raw.get("default_max_results"),
            5,
            label="[behavior] default_max_results",
            config_path=config_path,
        ),
        search_mode=_config._coerce_search_mode(
            behavior_raw.get("search_mode"), config_path=config_path
        ),
        conversational=_config._coerce_bool(behavior_raw.get("conversational"), True),
        recency_boost_half_life_days=_config._coerce_float(
            behavior_raw.get("recency_boost_half_life_days"),
            30.0,
            label="[behavior] recency_boost_half_life_days",
            config_path=config_path,
        ),
        write_supersession=_config._coerce_bool(
            behavior_raw.get("write_supersession"), True
        ),
        prompt_recall=_config._coerce_prompt_recall(
            behavior_raw.get("prompt_recall"), config_path=config_path
        ),
        session_brief=_config._coerce_bool(behavior_raw.get("session_brief"), True),
        heavily_used_min_applied=_config._coerce_int(
            behavior_raw.get("heavily_used_min_applied"),
            3,
            label="[behavior] heavily_used_min_applied",
            config_path=config_path,
        ),
        cold_endorsement_ratio_threshold=_config._coerce_float(
            behavior_raw.get("cold_endorsement_ratio_threshold"),
            0.0,
            label="[behavior] cold_endorsement_ratio_threshold",
            config_path=config_path,
        ),
        tombstone_retention_days=_config._coerce_int(
            behavior_raw.get("tombstone_retention_days"),
            0,
            label="[behavior] tombstone_retention_days",
            config_path=config_path,
        ),
        verification_stale_days=_config._coerce_int(
            behavior_raw.get("verification_stale_days"),
            30,
            label="[behavior] verification_stale_days",
            config_path=config_path,
        ),
        max_content_bytes=_config._coerce_int(
            behavior_raw.get("max_content_bytes"),
            1_000_000,
            label="[behavior] max_content_bytes",
            config_path=config_path,
        ),
        max_takeaway_bytes=_config._coerce_int(
            behavior_raw.get("max_takeaway_bytes"),
            4_096,
            label="[behavior] max_takeaway_bytes",
            config_path=config_path,
        ),
        max_scopes_per_write=_config._coerce_int(
            behavior_raw.get("max_scopes_per_write"),
            64,
            label="[behavior] max_scopes_per_write",
            config_path=config_path,
        ),
        min_content_tokens=_config._coerce_int(
            behavior_raw.get("min_content_tokens"),
            0,
            label="[behavior] min_content_tokens",
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
