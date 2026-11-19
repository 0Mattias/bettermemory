//! `verify_candidates.py`: the candidate logic path verification stands
//! on. Unit 1 (`validate`) holds the module's constants and the
//! validation of one candidate spelling, the leaves that need nothing
//! from the machine. The extraction scans, the body parse memo and the
//! checks that read the home directory or the disk are later units
//! (native/README.md, "Path candidates").
//!
//! Text is code points (`&[u32]`), as a Python `str` is: a body may hold
//! a lone surrogate, and every length and index here counts code points.

pub mod validate;

/// `_MAX_PATHS_PER_BODY`.
pub const MAX_PATHS_PER_BODY: usize = 8;
/// `_MAX_PATH_LENGTH`.
pub const MAX_PATH_LENGTH: usize = 512;
/// `_MAX_BODY_SCAN_BYTES`: a count of code points, despite its name.
pub const MAX_BODY_SCAN_BYTES: usize = 32 * 1024;
/// `_MAX_ANCHOR_CITATIONS`.
pub const MAX_ANCHOR_CITATIONS: usize = 24;
/// `_MAX_ANCHORED_CITATION_STATS`.
pub const MAX_ANCHORED_CITATION_STATS: usize = MAX_PATHS_PER_BODY;
/// `BODY_PARSE_ENTRIES`.
pub const BODY_PARSE_ENTRIES: usize = 4096;
/// `_TRAILING_PUNCT`.
pub const TRAILING_PUNCT: &str = ".,;:!?)>]}\"'";

/// `_WELLKNOWN_ROUTE_SEGMENTS`.
pub const WELLKNOWN_ROUTE_SEGMENTS: [&str; 9] = [
    "robots.txt",
    "favicon.ico",
    "sitemap.xml",
    "openapi.json",
    "index.html",
    "manifest.json",
    "humans.txt",
    "security.txt",
    "ads.txt",
];

/// `_PLACEHOLDER_PATHS`.
pub const PLACEHOLDER_PATHS: [&str; 8] = [
    "/etc/foo",
    "/etc/bar",
    "/etc/baz",
    "/foo",
    "/foo/bar",
    "/foo/baz",
    "/foo/bar/baz",
    "/path/to",
];

/// `_PLACEHOLDER_PREFIXES`, in the reference's order.
pub const PLACEHOLDER_PREFIXES: [&str; 2] = ["/path/to/", "~/path/to/"];

/// `_CHECKABLE_CITATION_EXTENSIONS`.
pub const CHECKABLE_CITATION_EXTENSIONS: [&str; 51] = [
    "py", "pyi", "ts", "tsx", "js", "jsx", "rs", "go", "rb", "java", "kt", "swift", "c", "h", "cc",
    "cpp", "hpp", "cs", "php", "lua", "sh", "sql", "vue", "svelte", "ipynb", "toml", "yaml", "yml",
    "json", "ini", "cfg", "conf", "env", "lock", "xml", "csv", "proto", "tf", "nix", "gradle",
    "plist", "service", "mod", "md", "mdx", "rst", "txt", "html", "css", "scss", "log",
];
