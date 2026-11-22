//! Cases a plausible wrong `location::capture` fails: the cache's clocks
//! and order, `S_ISDIR`'s `mode_t` on each platform, the token's statuses
//! and the order of the capture's steps.

mod location_capture_world;

use bettermemory_core::location::capture::{
    fresh, s_isdir, token_fields, AboveCache, Declined, MODE_T_BITS,
};
use location_capture_world::{cps, run_capture, st};
use serde_json::{json, Value};

#[test]
fn fresh_is_half_open_on_both_clocks() {
    assert!(fresh(0.0, 0.0, 0.0, 0.0));
    assert!(fresh(0.0, 0.0, 599.999, 599.999));
    assert!(!fresh(0.0, 0.0, 600.0, 1.0));
    assert!(!fresh(0.0, 0.0, 1.0, 600.0));
    assert!(!fresh(5.0, 0.0, 4.0, 1.0));
    assert!(!fresh(0.0, 5.0, 1.0, 4.0));
    assert!(!fresh(0.0, 0.0, f64::NAN, 1.0));
}

#[test]
fn a_key_set_again_keeps_its_place() {
    let cache = AboveCache::new(2);
    let put = |c: &AboveCache<u8>, k: &str| {
        let mut files = Files;
        let _ = bettermemory_core::location::capture::walk_files(
            &mut files,
            c,
            &mut || (1e9, 1e9),
            &mut |_| Ok(None),
            &cps("/x"),
            Some(&cps(k)),
        );
    };
    put(&cache, "/a");
    put(&cache, "/b");
    put(&cache, "/a");
    assert_eq!(cache.keys(), vec![cps("/a"), cps("/b")]);
    put(&cache, "/c");
    // The oldest by insertion, `/a`, goes, though it was used last.
    assert_eq!(cache.keys(), vec![cps("/b"), cps("/c")]);
}

struct Files;

impl bettermemory_core::location::capture::ProjectFiles for Files {
    type Above = u8;
    fn above_start(&mut self, _cwd: &[u32], root: &[u32]) -> Option<Vec<u32>> {
        Some(root.to_vec())
    }
    fn find(
        &mut self,
        _cwd: &[u32],
        _root: Option<&[u32]>,
        _above: Option<&u8>,
    ) -> bettermemory_core::location::capture::Walk<u8> {
        bettermemory_core::location::capture::Walk {
            files: vec![],
            errors: vec![],
            above: 0,
            folder: None,
            root: None,
        }
    }
}

#[test]
fn s_isdir_follows_mode_t() {
    // What CPython's `_stat` does: `mode_t` is 16 bits on macOS and
    // Windows (OverflowError past it), 32 on Linux.
    assert_eq!(s_isdir(0o040755, 16), Ok(true));
    assert_eq!(s_isdir(0o177777, 16), Ok(false));
    assert!(s_isdir(0o200000, 16).is_err());
    assert_eq!(s_isdir(0o240000, 32), Ok(true));
    assert_eq!(s_isdir(0o777777, 32), Ok(false));
    assert_eq!(s_isdir(u32::MAX, 32), Ok(false));
    if cfg!(target_os = "linux") {
        assert_eq!(MODE_T_BITS, 32);
    }
    if cfg!(any(target_os = "macos", windows)) {
        assert_eq!(MODE_T_BITS, 16);
    }
}

fn tf(state: &str, token: Option<&str>, write: bool, status: &str) -> (Value, bool) {
    let mut wrote = false;
    let got = token_fields((state.into(), token.map(cps)), write, &mut || {
        wrote = true;
        Ok::<_, Declined>((status.into(), Some(cps("W"))))
    })
    .unwrap();
    let s = |t: &Option<Vec<u32>>| t.as_ref().map_or(Value::Null, |t| json!(st(t)));
    (json!([s(&got.0), s(&got.1), st(&got.2)]), wrote)
}

#[test]
fn token_statuses() {
    assert_eq!(
        tf("present", None, true, "written"),
        (json!([null, null, "present"]), false)
    );
    assert_eq!(
        tf("odd", Some("T"), true, "written"),
        (json!([null, null, "failed:odd"]), false)
    );
    assert_eq!(
        tf("absent", None, false, "written"),
        (json!([null, null, "skipped:policy"]), false)
    );
    assert_eq!(
        tf("absent", None, true, "exists"),
        (json!(["W", null, "present"]), true)
    );
    assert_eq!(
        tf("absent", None, true, "written"),
        (json!([null, "W", "written"]), true)
    );
    assert_eq!(
        tf("absent", None, true, "skipped:bare"),
        (json!([null, null, "skipped:bare"]), true)
    );
}

fn full() -> Value {
    json!({
        "discover": {"dotgit": "/w/r/.git", "git_dir": "/g", "common_dir": "/c", "root": "/t", "kind": "primary"},
        "stats": {"/g/HEAD": "head", "/c/config": "config"},
        "files": ["/c/shallow"], "ticks": true,
    })
}

#[test]
fn shallow_is_read_before_the_ticks() {
    let row = json!({"world": full(), "write_token": true, "manifest": true});
    let (log, out) = run_capture(&row, 16);
    let at = |p: &str| log.iter().position(|l| l.starts_with(p)).unwrap();
    assert!(at("isfile") < at("capture_ticks"));
    assert!(at("token_read") < at("above_start"));
    assert!(at("read_remotes") < at("token_read"));
    assert_eq!(out["shallow"], json!(true));
    assert_eq!(out["ticks"], json!(true));
}

#[test]
fn an_empty_token_read_past_eviction_stays_not_read() {
    let mut w = full();
    w["evicted"] = json!(["head"]);
    w["stats"]["/g/token"] = json!("tok");
    w["token"] = json!(["present", ""]);
    let row = json!({"world": w, "write_token": true, "manifest": true});
    let (_, out) = run_capture(&row, 16);
    assert_eq!(out["token"], json!(""));
    assert_eq!(out["token_status"], json!("not_read:evicted"));
    assert_eq!(out["evicted"], json!(true));
    assert_eq!(out["kept_apart"], json!(false));
}

#[test]
fn an_empty_root_names_the_git_directory() {
    let mut w = full();
    w["discover"]["root"] = json!("");
    let row = json!({"world": w, "write_token": true, "manifest": true});
    let (log, out) = run_capture(&row, 16);
    assert_eq!(out["root"], json!("/g"));
    assert!(log.iter().any(|l| l == "remote_key u /g"));
}

#[test]
fn the_wide_mode_follows_the_bits_given() {
    let mut w = full();
    w["head"] = json!(["abc", null]);
    w["walk"] = json!({"files": [["bmp_1", "p", "/t", false]], "root": "/t"});
    w["commit"] = json!("tree");
    w["tree"] = json!([["bettermemory.toml", 0o200000]]);
    let row = json!({"world": w, "write_token": true, "manifest": true});
    assert_eq!(run_capture(&row, 32).1["file_tracked"], json!(true));
    assert_eq!(
        run_capture(&row, 16).1,
        json!({"raise": "OverflowError: mode out of range"})
    );
}

/// A walk that returns `next` as its part above and records the part
/// above it was given.
struct Scripted {
    next: u8,
    given: Vec<Option<u8>>,
}

impl bettermemory_core::location::capture::ProjectFiles for Scripted {
    type Above = u8;
    fn above_start(&mut self, _cwd: &[u32], root: &[u32]) -> Option<Vec<u32>> {
        Some(root.to_vec())
    }
    fn find(
        &mut self,
        _cwd: &[u32],
        _root: Option<&[u32]>,
        above: Option<&u8>,
    ) -> bettermemory_core::location::capture::Walk<u8> {
        self.given.push(above.copied());
        bettermemory_core::location::capture::Walk {
            files: vec![],
            errors: vec![],
            above: self.next,
            folder: None,
            root: None,
        }
    }
}

fn walk_at(cache: &AboveCache<u8>, files: &mut Scripted, t: f64) {
    let _ = bettermemory_core::location::capture::walk_files(
        files,
        cache,
        &mut || (t, t),
        &mut |_| Ok(None),
        &cps("/x"),
        Some(&cps("/r")),
    );
}

#[test]
fn a_hit_does_not_put_the_entry_again() {
    // `_ABOVE[start]` is set only where no fresh entry was found, so a
    // hit at 500 leaves the entry of 0, stale at 700.
    let cache = AboveCache::new(4);
    let mut files = Scripted {
        next: 1,
        given: vec![],
    };
    walk_at(&cache, &mut files, 0.0);
    walk_at(&cache, &mut files, 500.0);
    walk_at(&cache, &mut files, 700.0);
    assert_eq!(files.given, vec![None, Some(1), None]);
}

#[test]
fn a_stale_entry_put_again_holds_the_new_walk() {
    let cache = AboveCache::new(4);
    let mut files = Scripted {
        next: 1,
        given: vec![],
    };
    walk_at(&cache, &mut files, 0.0);
    files.next = 2;
    walk_at(&cache, &mut files, 700.0);
    walk_at(&cache, &mut files, 710.0);
    assert_eq!(files.given, vec![None, None, Some(2)]);
}

#[test]
fn a_cap_of_zero_holds_nothing() {
    let cache = AboveCache::new(0);
    let mut files = Scripted {
        next: 1,
        given: vec![],
    };
    walk_at(&cache, &mut files, 0.0);
    assert!(cache.keys().is_empty());
    walk_at(&cache, &mut files, 1.0);
    assert_eq!(files.given, vec![None, None]);
}
