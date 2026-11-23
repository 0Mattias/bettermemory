//! Real Python CLI projections, each ordered lookup, and both versions of the text rendering.
mod cli_membership_health_cases;
mod common;
#[test]
fn exact_model_sections_and_lines() {
    let rows = common::membership_rows::lines("cli_membership_health.jsonl");
    let mut ledger = common::parity::Ledger::new(
        "exact_model_sections_and_lines",
        "cli_membership_health.jsonl",
    );
    for (index, row) in rows.iter().enumerate() {
        let matched = cli_membership_health_cases::scripted(row);
        assert!(
            !ledger.differs_fatally(format!("row {index} ordered reads"), matched),
            "row {index} ordered reads"
        );
    }
    ledger.finish(rows.len());
    assert!(rows.len() >= 22);
}

#[cfg(unix)]
#[test]
fn real_signed_membership_is_read_without_writes() {
    use bettermemory_core::{
        cli::membership_health::{checkout_path, health_model, CheckoutError},
        location_caller::ProcessMachine,
        machine::{MachineFacts, MachineIdentity},
        membership::{Error, LivenessSeam, MembershipStore, Options, Query, Written},
        pyvalue::{PyStr, PyValue},
        store::{membership::membership_rows, open::open_for_writing},
        unicode::cps,
    };
    use common::{
        membership_rows::{dec, field, list, name},
        opened,
        stores::scratch,
        written,
    };
    use rusqlite::Connection;
    struct ReadOnly(Connection);
    impl MembershipStore for ReadOnly {
        type Error = String;
        fn in_batch(&self) -> bool {
            !self.0.is_autocommit()
        }
        fn batch<T>(
            &self,
            _: &mut dyn FnMut() -> Result<T, Error<String>>,
        ) -> Result<T, Error<String>> {
            panic!("unexpected transaction")
        }
        fn put_membership(
            &self,
            _: &[u32],
            _: &PyValue,
            _: Option<&[u32]>,
        ) -> Result<Written, String> {
            panic!("unexpected write")
        }
        fn delete_membership(
            &self,
            _: &[u32],
            _: &PyValue,
            _: Option<&[u32]>,
        ) -> Result<Written, String> {
            panic!("unexpected delete")
        }
        fn record_membership(
            &self,
            _: &[u32],
            _: &PyValue,
            _: Option<&[u32]>,
        ) -> Result<Written, String> {
            panic!("unexpected event")
        }
        fn next_membership_id(&self, _: &str, _: &str) -> Result<PyStr, String> {
            panic!("unexpected mint")
        }
        fn next_membership_number(&self, _: &str, _: &str) -> Result<i64, String> {
            panic!("unexpected counter")
        }
        fn membership_rows(&self, t: &str, q: &Query) -> Result<Vec<PyValue>, String> {
            membership_rows(
                &self.0,
                t,
                q.where_.as_deref(),
                q.column_in
                    .as_ref()
                    .map(|(k, v)| (k.as_slice(), v.as_slice())),
                &q.order_by,
            )
            .map_err(|e| e.to_string())
        }
    }
    let rows = common::membership_rows::lines("cli_membership_health.jsonl");
    let row = rows
        .iter()
        .find(|r| r.get("real").is_some())
        .expect("real row");
    let dir = scratch("cli-health-signed");
    let root = dir.0.join("section");
    opened::made("created", &root);
    let file = root.join("memory.sqlite");
    let mut clocks = opened::engine_provider();
    clocks.clock = (0..15)
        .map(|i| written::instant(&format!("2003-04-05T06:07:{:02}Z", 8 + i)))
        .collect();
    let mut store = open_for_writing(&file, &root.join("keys"), &clocks).unwrap();
    let writes = dec(field(row, "writes"));
    for args in list(&writes) {
        let a = list(args);
        let PyValue::Dict(columns) = &a[1] else {
            unreachable!()
        };
        store
            .put_membership(&mut clocks, &cps(&name(&a[0])), columns, None)
            .unwrap();
    }
    store
        .record_membership(&mut clocks, &cps("resolution_deferred"), &[], None)
        .unwrap();
    let observed = || {
        let db = Connection::open(&file).unwrap();
        let mut read = db.prepare("SELECT seq,mac FROM log ORDER BY seq").unwrap();
        let records = read
            .query_map([], |r| {
                Ok(PyValue::List(vec![
                    PyValue::int(i128::from(r.get::<_, i64>(0)?)),
                    PyValue::str(&r.get::<_, String>(1)?),
                ]))
            })
            .unwrap();
        PyValue::List(records.collect::<Result<Vec<_>, _>>().unwrap())
    };
    assert_eq!(&observed(), field(row, "signed"));
    let process = ProcessMachine::new();
    let options = || Options {
        liveness: LivenessSeam::Given(Box::new(|_, _| cps("gone"))),
        token_writer: None,
        now: Box::new(|| panic!("unexpected clock")),
        session: None,
    };
    let open = || {
        Ok::<_, String>(MachineIdentity {
            id: cps("M1"),
            facts: MachineFacts {
                hardware: None,
                hostname: None,
                id_file: None,
            },
            predecessor: None,
            minted: false,
            cloned: false,
            facts_updated: false,
            recorded: None,
        })
    };
    let model = health_model(
        ReadOnly(Connection::open(&file).unwrap()),
        &process,
        open,
        options(),
    )
    .unwrap();
    assert_eq!(model.machine, cps("M1"));
    let other = health_model(
        ReadOnly(Connection::open(&file).unwrap()),
        &process,
        || -> Result<MachineIdentity, String> { panic!("identity must be shared") },
        options(),
    )
    .unwrap();
    assert_eq!(model.machine, other.machine);
    assert!(cli_membership_health_cases::check(row, |id| {
        checkout_path(&model, id, |msg| {
            CheckoutError::Raised(cli_membership_health_cases::exc("StoreError", cps(&msg)))
        })
    }));
    assert_eq!(&observed(), field(row, "signed"));
}
