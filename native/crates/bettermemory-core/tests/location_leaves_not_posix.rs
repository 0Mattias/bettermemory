//! Off POSIX `location_leaves_posix.jsonl` is not made (its trees hold
//! links), and the Windows flavour resolves no path (`realpath` is not
//! ported there, ENG-124): `local` and `real_git` decline. Class os.

#![cfg(not(unix))]

use bettermemory_core::fsdata::realpath::Unresolved;
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::location::leaves::local;

#[test]
fn the_windows_flavour_declines_a_local_key() {
    let path: Vec<u32> = "C:\\x".chars().map(u32::from).collect();
    let got = local(&StdFs, Flavour::Windows, b"C:\\", &path, None);
    assert_eq!(got, Err(Unresolved::NotPorted));
    eprintln!("PARITY location_leaves_not_posix: 0 compared, 1 set aside (os: no links, realpath not ported)");
}
