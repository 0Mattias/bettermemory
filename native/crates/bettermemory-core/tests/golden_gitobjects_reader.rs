//! Every case of `gitobjects_reader.jsonl` (class i) and
//! `gitobjects_reader_zlib.jsonl` (class ii, zlib) against
//! `gitobjects::files::reader`: `has`, `has_many`, `resolve`, `header`,
//! `read`, `commit`, `tree`, `fingerprint` and `name_length` over object
//! stores written by hand. Each case is asked in memory, and on POSIX on
//! disk too.

mod common;

#[test]
fn gitobjects_reader_every_case() {
    common::gitobjects_reader::every_case("golden_gitobjects_reader", "gitobjects_reader.jsonl");
}

#[test]
fn gitobjects_reader_zlib_every_case() {
    common::gitobjects_reader::every_case(
        "golden_gitobjects_reader",
        "gitobjects_reader_zlib.jsonl",
    );
}
