use std::fs;
use std::path::Path;

fn main() {
    // Cargo runs a build script in its package directory, including for
    // an install from another working directory. The complete source tree
    // contains the same project manifest used to build the Python wheel.
    let path = Path::new("../../../pyproject.toml");
    println!("cargo::rerun-if-changed={}", path.display());
    let source = fs::read_to_string(path)
        .expect("building bettermemory requires the source tree with its root pyproject.toml");
    let manifest: toml::Table = source.parse().expect("invalid root pyproject.toml");
    let project = manifest
        .get("project")
        .and_then(toml::Value::as_table)
        .expect("missing [project] in root pyproject.toml");
    assert_eq!(
        project.get("name").and_then(toml::Value::as_str),
        Some("bettermemory"),
        "the root pyproject.toml must describe bettermemory"
    );
    let version = project
        .get("version")
        .and_then(toml::Value::as_str)
        .expect("missing [project].version string in root pyproject.toml");
    assert!(
        !version.is_empty() && !version.contains(['\r', '\n', '\0']),
        "invalid [project].version in root pyproject.toml"
    );
    println!("cargo::rustc-env=BETTERMEMORY_VERSION={version}");
}
