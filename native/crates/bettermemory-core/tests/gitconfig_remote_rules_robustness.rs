//! Hostile names, values and refspecs for the remote rules: no panic and
//! an answer for 10 MB inputs, which each rule reads in one pass.

use bettermemory_core::gitconfig::remote_rules::{refname_valid, refspec_valid, remote_rule};
use bettermemory_core::gitconfig::spelled;

const TEN_MB: usize = 10 << 20;

#[test]
fn ten_megabyte_names_and_refspecs_answer() {
    let long = b"a/".repeat(TEN_MB / 2);
    assert!(!refname_valid(&long, true, true));
    let ok = [b"refs/".to_vec(), b"ab/".repeat(TEN_MB / 3), b"c".to_vec()].concat();
    assert!(refname_valid(&ok, false, false));
    let dots = b".".repeat(TEN_MB);
    assert!(!refname_valid(&dots, false, true));
    let stars = b"*".repeat(TEN_MB);
    assert!(!refname_valid(&stars, true, true));
    let colons = b":".repeat(TEN_MB);
    assert_eq!(refspec_valid(&colons, true), Some(false));
    assert_eq!(refspec_valid(&colons, false), Some(false));
    let spec = [b"+".to_vec(), ok.clone(), b":".to_vec(), ok.clone()].concat();
    assert_eq!(refspec_valid(&spec, true), Some(true));
    let hex = [b"^".to_vec(), b"0".repeat(64)].concat();
    assert_eq!(refspec_valid(&hex, true), None);
}

#[test]
fn hostile_names_answer() {
    let name = [b"remote.".to_vec(), vec![0xff; TEN_MB], b".fetch".to_vec()].concat();
    let value = [b"+".to_vec(), b"x".repeat(TEN_MB), b":".to_vec()].concat();
    assert_eq!(remote_rule(&spelled(&name), Some(&value)), None);
    assert_eq!(
        remote_rule(&spelled(&name), Some(b"a..b")),
        Some("a refspec git refuses")
    );
    let dotted = b".".repeat(TEN_MB);
    assert_eq!(remote_rule(&spelled(&dotted), None), None);
    let bool_value = b"9".repeat(TEN_MB);
    assert_eq!(
        remote_rule(&spelled(b"remote.o.prune"), Some(&bool_value)),
        Some("no boolean")
    );
}
