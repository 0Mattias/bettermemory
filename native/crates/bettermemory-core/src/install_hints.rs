//! The install-command spellings of `_install_hints.py` (lines 41-130),
//! each a string exactly as the reference's f-string spells it.

/// `_TOOL_REINSTALL`: the leading command.
pub const TOOL_REINSTALL: &str = "uv tool install --reinstall";

/// `extras_spec`: the quoted package-plus-extra spec.
pub fn extras_spec(extra: &str) -> String {
    format!("'bettermemory[{extra}]'")
}

/// `tool_reinstall`.
pub fn tool_reinstall(extra: &str) -> String {
    format!("{TOOL_REINSTALL} {}", extras_spec(extra))
}

/// `tool_reinstall_package`.
pub fn tool_reinstall_package() -> String {
    format!("{TOOL_REINSTALL} bettermemory")
}

/// `pipx_force`.
pub fn pipx_force(extra: &str) -> String {
    format!("pipx install --force {}", extras_spec(extra))
}

/// `pip_force_reinstall`.
pub fn pip_force_reinstall(module: &str) -> String {
    format!("uv pip install --force-reinstall {module}")
}

/// `dev_clone_editable`.
pub fn dev_clone_editable(extra: &str) -> String {
    format!("uv pip install -e \".[{extra}]\"")
}

/// `install_extra_command`.
pub fn install_extra_command(extra: &str) -> String {
    format!(
        "`{}` (pipx: `{}`; from a development clone: `{}`)",
        tool_reinstall(extra),
        pipx_force(extra),
        dev_clone_editable(extra)
    )
}

/// `reinstall_extra_command`.
pub fn reinstall_extra_command(module: &str, extra: &str) -> String {
    format!(
        "`{}` (pipx: `{}`; inside the virtualenv that runs bettermemory: `{}`)",
        tool_reinstall(extra),
        pipx_force(extra),
        pip_force_reinstall(module)
    )
}
