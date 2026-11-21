//! Every case of `install_hints.jsonl` (not gated) against
//! `install_hints`: each function's text for each argument.

mod common;

use bettermemory_core::install_hints as ih;
use common::parity::Ledger;

#[test]
fn install_hints_every_case() {
    let file = "install_hints.jsonl";
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert_eq!(lines[0]["tool_reinstall"], ih::TOOL_REINSTALL);
    let mut ledger = Ledger::new("install_hints_every_case", file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(format!("line {} {}", i + 2, r), || {
            let a: Vec<&str> = r["a"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap())
                .collect();
            let got = match r["f"].as_str().unwrap() {
                "tool_reinstall_package" => ih::tool_reinstall_package(),
                "extras_spec" => ih::extras_spec(a[0]),
                "tool_reinstall" => ih::tool_reinstall(a[0]),
                "pipx_force" => ih::pipx_force(a[0]),
                "dev_clone_editable" => ih::dev_clone_editable(a[0]),
                "install_extra_command" => ih::install_extra_command(a[0]),
                "pip_force_reinstall" => ih::pip_force_reinstall(a[0]),
                "reinstall_extra_command" => ih::reinstall_extra_command(a[0], a[1]),
                other => panic!("unknown function {other}"),
            };
            assert_eq!(got, r["r"].as_str().unwrap());
        });
    }
    ledger.finish(rows.len());
}
