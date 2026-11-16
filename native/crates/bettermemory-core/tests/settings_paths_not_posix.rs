//! `settings_paths.json` and `settings_paths_python.json` have no case
//! off POSIX: their generators expand, resolve and print paths by POSIX
//! rules (`~name`, the root, `//`, a NUL), which the native tests' world
//! reproduces, and the reference's paths differ on Windows, where the
//! files are not generated. `golden_settings.rs` reads them on unix only;
//! here the gap is recorded, so the parity table names it.

#![cfg(not(unix))]

mod common;

#[test]
fn the_settings_path_fixtures_have_no_case_here() {
    for (test, file) in [
        ("golden_settings_paths", "settings_paths.json"),
        ("golden_settings_paths_python", "settings_paths_python.json"),
    ] {
        common::parity::record(
            test,
            file,
            "os",
            0,
            &["every case of the fixture".to_owned()],
            &["the fixture's paths follow POSIX rules; Windows has no case".to_owned()],
        );
    }
}
