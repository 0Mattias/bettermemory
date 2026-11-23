//! File labels in Python strings. Paths from a real POSIX process can contain
//! surrogateescaped bytes, including inside a warning or a one-shot guard.
use super::{Config, Ctx, Raised, Source};
use crate::pyvalue::PyStr;
use crate::unicode::{cps, to_string};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError {
    pub class: &'static str,
    pub message: PyStr,
}
impl ConfigError {
    pub(crate) fn located(prefix: &str, path: &[u32], suffix: &str) -> Self {
        Self {
            class: "ValueError",
            message: [cps(prefix), path.to_vec(), cps(suffix)].concat(),
        }
    }
    pub(crate) fn narrow(self) -> Raised {
        Raised {
            class: self.class,
            message: scalar(&self.message),
        }
    }
}
impl From<Raised> for ConfigError {
    fn from(value: Raised) -> Self {
        Self {
            class: value.class,
            message: cps(&value.message),
        }
    }
}
fn scalar(value: &[u32]) -> String {
    to_string(value).expect("a source supplied through the UTF-8 config API")
}

pub(crate) struct Labels {
    pub(crate) text: PyStr,
    pub(crate) resolved: PyStr,
    pub(crate) is_file: bool,
}
impl From<&Source<'_>> for Labels {
    fn from(s: &Source<'_>) -> Self {
        Self {
            text: cps(s.text),
            resolved: cps(&s.resolved),
            is_file: s.is_file,
        }
    }
}
impl Config<PyStr> {
    pub(crate) fn scalar_paths(self) -> Config {
        Config {
            values: self.values,
            config_path: self.config_path.map(|p| scalar(&p)),
            managed_path: self.managed_path.map(|p| scalar(&p)),
            managed_keys: self.managed_keys,
            managed_fixed: self.managed_fixed,
        }
    }
}
pub(crate) trait Notices {
    fn warn(&mut self, message: PyStr) -> Result<(), ConfigError>;
    fn guard(&mut self, source: &[u32], key: PyStr) -> bool;
    fn note(&mut self, message: String);
}
impl Notices for Ctx<'_> {
    fn warn(&mut self, message: PyStr) -> Result<(), ConfigError> {
        self.log.push(scalar(&message));
        Ok(())
    }
    fn guard(&mut self, source: &[u32], key: PyStr) -> bool {
        self.guards.insert((scalar(source), scalar(&key)))
    }
    fn note(&mut self, _: String) {}
}
pub(super) struct Trial<'a> {
    pub(super) clean: bool,
    pub(super) outer: &'a mut dyn Notices,
}
impl Notices for Trial<'_> {
    fn warn(&mut self, _: PyStr) -> Result<(), ConfigError> {
        self.clean = false;
        Ok(())
    }
    fn guard(&mut self, source: &[u32], key: PyStr) -> bool {
        self.outer.guard(source, key)
    }
    fn note(&mut self, _: String) {
        self.clean = false;
    }
}
