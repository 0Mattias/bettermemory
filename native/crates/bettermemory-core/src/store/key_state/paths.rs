//! A Path's display and equality retain its Python spelling until a filesystem call.
use super::{StateError, StoreError};
use crate::fsutil::fsdecode;
#[cfg(unix)]
use crate::log::ring::binding_path::HOST;
use crate::pyvalue::PyStr;
use crate::unicode::cps;

#[cfg(unix)]
pub(crate) fn normalize_spelling(text: &[u32]) -> PyStr {
    crate::verify_anchoring::attest::pure_text(HOST, text).expect("POSIX Path parsing is total")
}

/// The filename is a validated store ID and a fixed suffix, so it is a single component.
pub(crate) fn ring_file(dir: &[u32], store_id: &str, suffix: &str) -> PyStr {
    let name = cps(&format!("{store_id}{suffix}"));
    if dir == [u32::from(b'.')] {
        name
    } else {
        #[cfg(unix)]
        {
            crate::gitfile_rule::hostpath::text::join(HOST, dir, &[&name])
        }
        #[cfg(windows)]
        windows::join_name(dir, &name)
    }
}

/// Non-strict POSIX realpath resolves one component at a time. Original names
/// keep their spelling; only a link target comes back decoded from host bytes.
#[cfg(unix)]
pub(crate) fn resolve_spelling(text: &[u32]) -> Result<PyStr, StateError> {
    use super::super::key_read::{os_path, FilePathError};
    use std::collections::HashMap;

    const SEP: u32 = b'/' as u32;
    const DOT: u32 = b'.' as u32;
    enum Part {
        Name(PyStr),
        EndLink(PyStr),
    }
    let names = |p: &[u32]| -> Vec<Part> {
        p.split(|&cp| cp == SEP)
            .rev()
            .filter(|s| !s.is_empty() && *s != [DOT])
            .map(|s| Part::Name(s.to_vec()))
            .collect()
    };
    let mut path = if text.first() == Some(&SEP) {
        vec![SEP]
    } else {
        fsdecode(std::env::current_dir().map_err(StoreError::Io)?.as_os_str())
    };
    let mut parts = names(text);
    let mut links: HashMap<PyStr, Option<PyStr>> = HashMap::new();
    while let Some(part) = parts.pop() {
        let name = match part {
            Part::EndLink(link) => {
                links.insert(link, Some(path.clone()));
                continue;
            }
            Part::Name(name) => name,
        };
        if name == [DOT, DOT] {
            let last = path.iter().rposition(|&cp| cp == SEP).unwrap_or(0);
            path.truncate(last.max(1));
            continue;
        }
        let parent_len = path.len();
        if path.len() > 1 {
            path.push(SEP);
        }
        path.extend(name);
        let host = os_path(&path).map_err(|e| match e {
            FilePathError::Nul => {
                StateError::raised("ValueError", "lstat: embedded null character in path")
            }
            _ => StateError::Raised {
                class: e.class(),
                message: e.message(),
            },
        })?;
        if !std::fs::symlink_metadata(&host).is_ok_and(|m| m.is_symlink()) {
            continue;
        }
        if let Some(seen) = links.get(&path) {
            if let Some(target) = seen {
                path.clone_from(target);
            }
            continue;
        }
        let Ok(target) = std::fs::read_link(&host) else {
            continue;
        };
        let target = fsdecode(target.as_os_str());
        let link = path.clone();
        links.insert(link.clone(), None);
        path.truncate(if target.first() == Some(&SEP) {
            1
        } else {
            parent_len
        });
        parts.push(Part::EndLink(link));
        parts.extend(names(&target));
    }
    Ok(path)
}

#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub(crate) use windows::{normalize_spelling, resolve_spelling};

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::log::ring::KeyFiles;
    use crate::pyvalue::PyValue;
    use crate::store::key_state::{
        warn_bound_elsewhere, warn_bound_exact, warn_unclaimed_exact, KeyState, KeyStatus, Level,
        WarningError,
    };
    use std::os::unix::fs::symlink;
    use std::path::{Path, PathBuf};

    struct Temp(PathBuf);
    impl Drop for Temp {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn temp() -> Temp {
        let tmp = std::env::temp_dir().join(format!(
            "bm-spelling-{}-{:016x}",
            std::process::id(),
            getrandom::u64().unwrap()
        ));
        std::fs::create_dir(&tmp).unwrap();
        Temp(std::fs::canonicalize(tmp).unwrap())
    }
    fn contains(s: &[u32], part: &[u32]) -> bool {
        s.windows(part.len()).any(|w| w == part)
    }

    #[test]
    fn spelled_views_and_shared_warning_keys() {
        let tmp = temp();
        let base = fsdecode(tmp.0.as_os_str());
        let prefix = [base, cps("/")].concat();
        let accents = [0xdcc3, 0xdca9];
        let alias = [prefix.clone(), accents.to_vec()].concat();
        let real = tmp.0.join("é");
        std::fs::create_dir(&real).unwrap();
        symlink("é", tmp.0.join("link")).unwrap();
        let via = |name: &str| fsdecode(tmp.0.join(name).as_os_str());
        assert_eq!(resolve_spelling(&alias).unwrap(), alias);
        assert_eq!(resolve_spelling(&via("link")).unwrap(), via("é"));
        let leaf = [via("link/"), cps("/"), accents.to_vec()].concat();
        assert_eq!(
            resolve_spelling(&leaf).unwrap(),
            [via("é"), cps("/"), accents.to_vec()].concat()
        );
        assert_eq!(
            resolve_spelling(&[alias.clone(), cps("/../link")].concat()).unwrap(),
            via("é")
        );
        let ring = KeyFiles::new(&real, &PyValue::str("124578ab124578ab124578ab124578ab")).unwrap();
        let mut facts = KeyState::new(
            KeyStatus::BoundElsewhere,
            ring,
            real.clone(),
            cps("fingerprint"),
        );
        let peer = via("other");
        let view = facts.spelled(&alias, &alias, Some(&peer));
        assert_eq!(view.withheld(), Some(peer.clone()));
        assert!(contains(&view.summary().unwrap(), &alias));
        assert!(contains(&view.remedy().unwrap().unwrap(), &alias));
        assert!(contains(&view.refusal(&alias).unwrap(), &alias));
        let PyValue::Dict(ref fields) = view.details().unwrap() else {
            panic!("details")
        };
        assert!(fields.contains(&(
            PyValue::str("key_path"),
            PyValue::Str(ring_file(&alias, facts.keyring.store_id(), ".key"))
        )));
        assert!(fields.contains(&(PyValue::str("bound_to"), PyValue::Str(peer.clone()))));
        let mut levels = Vec::new();
        for path in [&alias, &via("é"), &via("link")] {
            warn_bound_exact(&facts, path, &alias, &peer, |level, summary, remedy| {
                levels.push(level);
                assert!(contains(&summary, &alias));
                assert_eq!(remedy[0], u32::from(b'M'));
                Ok::<_, ()>(())
            })
            .unwrap();
        }
        warn_bound_elsewhere(
            &facts.keyring,
            &real,
            &cps(facts.keyring.store_id()),
            &tmp.0.join("other"),
            None,
            |w| {
                levels.push(w.level);
                Ok::<_, ()>(())
            },
        )
        .unwrap();
        assert_eq!(
            levels,
            [Level::Warning, Level::Warning, Level::Debug, Level::Debug]
        );

        facts.state = KeyStatus::Unclaimed;
        let binding = ring_file(&alias, facts.keyring.store_id(), ".path");
        assert_eq!(
            facts.spelled(&alias, &alias, None).withheld(),
            Some(binding)
        );
        for (dir, expected) in [
            (&alias, Level::Warning),
            (&via("é"), Level::Warning),
            (&alias, Level::Debug),
        ] {
            let result = warn_unclaimed_exact(&facts, &peer, dir, |level, _, _| {
                assert_eq!(level, expected);
                Err("logger")
            });
            assert!(matches!(result, Err(WarningError::Logger("logger"))));
        }
    }

    #[test]
    fn resolution_error_uses_the_first_looked_up_component() {
        let invalid = [cps("/first/"), vec![0xd800], cps("/last")].concat();
        let Err(StateError::Raised { class, message }) = resolve_spelling(&invalid) else {
            panic!("missing encoding failure");
        };
        assert_eq!(class, "UnicodeEncodeError");
        assert!(contains(&message, &cps("position 7")));
        assert!(matches!(
            resolve_spelling(&cps("/first/bad\0/last")),
            Err(StateError::Raised {
                class: "ValueError",
                ..
            })
        ));
        assert_eq!(normalize_spelling(&cps("//a/./b/")), cps("//a/b"));
        assert_eq!(super::super::path_text(Path::new("//a/./b/")), cps("//a/b"));
    }
}
