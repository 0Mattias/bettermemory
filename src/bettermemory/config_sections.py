"""The section builders of ``config._config_from_data``.

``_config_from_data`` reads each section's table from one parsed TOML
file and builds the ``Config``. The longer sections are built here, one
function each: it takes the section's table and the file it came from
(the user's config.toml, or the managed file or registry value) and
returns the section's settings, coerced by ``config``'s helpers with the
same defaults, labels and notices. ``config`` imports these when a file
is read, since this module reads its names. ``_coerce_int``, which tests
patch on ``config``, is read from it at call time.
"""

from __future__ import annotations

from typing import Any

from .config import (
    BehaviorConfig,
    CaptureConfig,
    ConfigSource,
    PolicyConfig,
    ScopesConfig,
    StorageConfig,
    _clamp_checkpoint_tokens,
    _coerce_acknowledge_names,
    _coerce_bool,
    _coerce_float,
    _coerce_optional_str,
    _coerce_prompt_recall,
    _coerce_search_mode,
    _coerce_str_list,
    _coerce_switch,
)


def _storage_config(
    storage_raw: dict[str, Any], config_path: ConfigSource
) -> StorageConfig:
    """Coerce the [storage] table; takes it and the file it came from and returns the section's settings."""
    return StorageConfig(
        directory=_coerce_optional_str(
            storage_raw.get("directory"),
            label="[storage] directory",
            config_path=config_path,
        ),
        cwd_store=_coerce_switch(
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
    from .config import _coerce_int

    return BehaviorConfig(
        default_max_results=_coerce_int(
            behavior_raw.get("default_max_results"),
            5,
            label="[behavior] default_max_results",
            config_path=config_path,
        ),
        search_mode=_coerce_search_mode(
            behavior_raw.get("search_mode"), config_path=config_path
        ),
        conversational=_coerce_bool(behavior_raw.get("conversational"), True),
        recency_boost_half_life_days=_coerce_float(
            behavior_raw.get("recency_boost_half_life_days"),
            30.0,
            label="[behavior] recency_boost_half_life_days",
            config_path=config_path,
        ),
        write_supersession=_coerce_bool(behavior_raw.get("write_supersession"), True),
        prompt_recall=_coerce_prompt_recall(
            behavior_raw.get("prompt_recall"), config_path=config_path
        ),
        session_brief=_coerce_bool(behavior_raw.get("session_brief"), True),
        heavily_used_min_applied=_coerce_int(
            behavior_raw.get("heavily_used_min_applied"),
            3,
            label="[behavior] heavily_used_min_applied",
            config_path=config_path,
        ),
        cold_endorsement_ratio_threshold=_coerce_float(
            behavior_raw.get("cold_endorsement_ratio_threshold"),
            0.0,
            label="[behavior] cold_endorsement_ratio_threshold",
            config_path=config_path,
        ),
        tombstone_retention_days=_coerce_int(
            behavior_raw.get("tombstone_retention_days"),
            0,
            label="[behavior] tombstone_retention_days",
            config_path=config_path,
        ),
        verification_stale_days=_coerce_int(
            behavior_raw.get("verification_stale_days"),
            30,
            label="[behavior] verification_stale_days",
            config_path=config_path,
        ),
        max_content_bytes=_coerce_int(
            behavior_raw.get("max_content_bytes"),
            1_000_000,
            label="[behavior] max_content_bytes",
            config_path=config_path,
        ),
        max_takeaway_bytes=_coerce_int(
            behavior_raw.get("max_takeaway_bytes"),
            4_096,
            label="[behavior] max_takeaway_bytes",
            config_path=config_path,
        ),
        max_scopes_per_write=_coerce_int(
            behavior_raw.get("max_scopes_per_write"),
            64,
            label="[behavior] max_scopes_per_write",
            config_path=config_path,
        ),
        min_content_tokens=_coerce_int(
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
    return ScopesConfig(
        allowed=_coerce_str_list(
            scopes_raw.get("allowed"),
            label="[scopes] allowed",
            config_path=config_path,
        ),
        typo_exceptions=_coerce_str_list(
            scopes_raw.get("typo_exceptions"),
            label="[scopes] typo_exceptions",
            config_path=config_path,
        ),
    )


def _policy_config(
    policy_raw: dict[str, Any], config_path: ConfigSource
) -> PolicyConfig:
    """Coerce the [policy] table; takes it and the file it came from and returns the section's settings."""
    return PolicyConfig(
        forbid_force=_coerce_switch(
            policy_raw.get("forbid_force"),
            False,
            label="[policy] forbid_force",
            config_path=config_path,
        ),
        forbid_acknowledge=_coerce_acknowledge_names(
            policy_raw.get("forbid_acknowledge"),
            label="[policy] forbid_acknowledge",
            config_path=config_path,
        ),
    )


def _capture_config(
    capture_raw: dict[str, Any], config_path: ConfigSource
) -> CaptureConfig:
    """Coerce the [capture] table; takes it and the file it came from and returns the section's settings."""
    from .config import _coerce_int

    return CaptureConfig(
        stop_checkpoint=_coerce_bool(capture_raw.get("stop_checkpoint"), False),
        checkpoint_tokens=_clamp_checkpoint_tokens(
            _coerce_int(
                capture_raw.get("checkpoint_tokens"),
                12_000,
                label="[capture] checkpoint_tokens",
                config_path=config_path,
            ),
            config_path=config_path,
        ),
        daily_cap=_coerce_int(
            capture_raw.get("daily_cap"),
            12,
            label="[capture] daily_cap",
            config_path=config_path,
        ),
    )
