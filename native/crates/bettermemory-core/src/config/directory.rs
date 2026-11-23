//! The directory precedence of `Config.resolved_directory`, shared by real
//! read-only commands and the config path report's supplied filesystem.
use super::{Config, Setting, ENV_DIR_OVERRIDE};

pub(crate) fn select<P, T, E>(
    config: &Config<P>,
    environment: Option<T>,
    from_config: impl FnOnce(&str) -> T,
    named: impl FnOnce(&'static str, T) -> Result<T, E>,
    project: impl FnOnce() -> Result<Option<T>, E>,
    home: impl FnOnce() -> Result<T, E>,
) -> Result<T, E> {
    if let Some(path) = environment {
        return named(ENV_DIR_OVERRIDE, path);
    }
    if let Setting::Str(directory) = config.get("storage", "directory") {
        if !directory.is_empty() {
            return named("[storage] directory", from_config(directory));
        }
    }
    if config.get("storage", "cwd_store").as_bool() {
        if let Some(path) = project()? {
            return Ok(path);
        }
    }
    home()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{config_from_data, Ctx, Guards, Source};
    use crate::toml;

    #[test]
    fn disabled_project_does_not_ask_for_cwd() {
        let mut guards = Guards::new();
        let mut data = toml::loads("[storage]\ncwd_store=false").unwrap();
        let config =
            config_from_data(&mut data, &Source::registry(), &mut Ctx::new(&mut guards)).unwrap();
        let got = select(
            &config,
            None,
            str::to_owned,
            |_, _| panic!("unexpected configured directory"),
            || panic!("directory selection consulted cwd with cwd_store=false"),
            || Ok::<_, ()>("home".to_owned()),
        )
        .unwrap();
        assert_eq!(got, "home");
    }
}
