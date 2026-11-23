//! `bm-verify [--fold] <store file> <keys dir>`: open a store read-only, apply the
//! version contract, count each table's rows, read the key ring and verify
//! the chain on disk; print ONE JSON object with the outcome, the counts,
//! the chain report and each phase's time in milliseconds.
//!
//! It prints no record content, no payload text and no key material: the
//! report's fields are seqs, statuses, log row kinds, MACs, key
//! fingerprints and counts. Exit status 0 when the chain is `ok`, 1 for any
//! other report, 2 when the store could not be read.
//!
//! With `--fold` the chain and the fold check run on one state of the log
//! (`StoreFile::log_verify_loading`) and it prints `log verify --json`'s
//! report under `log_verify` (every key but `path`) with its time; the exit
//! status is then the merged report's (3: the store kept changing). The fold's labels
//! are record ids and table keys; a payload problem's detail is the
//! reference's exception text, which can quote a payload value.

use std::path::Path;
use std::time::Instant;

use bettermemory_core::log::{KeyRing, ReportDict, ReportValue, VerifyError};
use bettermemory_core::store::fold::{self, Cell, Label, Problem, TableFold};
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::{sqlite_version, StoreError, StoreFile};
use bettermemory_core::unicode;

/// The reference version the version contract's messages name.
const VERSION: &str = "9.0.0";

fn quote(text: &str) -> String {
    let mut out = String::from("\"");
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 || (c as u32) >= 0x7f => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn quote_cps(cps: &[u32]) -> String {
    match unicode::to_string(cps) {
        Some(text) => quote(&text),
        None => {
            let mut out = String::from("\"");
            for &cp in cps {
                match char::from_u32(cp) {
                    Some(c) => {
                        let q = quote(&c.to_string());
                        out.push_str(&q[1..q.len() - 1]);
                    }
                    None => out.push_str(&format!("\\u{cp:04x}")),
                }
            }
            out.push('"');
            out
        }
    }
}

fn value(v: &ReportValue) -> String {
    match v {
        ReportValue::Null => "null".into(),
        ReportValue::Int(i) => i.to_string(),
        ReportValue::BigInt(t) => t.clone(),
        ReportValue::Str(s) => quote_cps(s),
    }
}

fn dict(d: &ReportDict) -> String {
    let fields: Vec<String> = d
        .iter()
        .map(|(k, v)| format!("{}: {}", quote(k), value(v)))
        .collect();
    format!("{{{}}}", fields.join(", "))
}

fn ms(start: Instant) -> String {
    format!("{:.3}", start.elapsed().as_secs_f64() * 1000.0)
}

fn fail(fields: &mut Vec<String>, phase: &str, error: &str) -> i32 {
    fields.push(format!(
        "{}: {}",
        quote("error"),
        quote(&format!("{phase}: {error}"))
    ));
    println!("{{{}}}", fields.join(", "));
    2
}

fn main() {
    // Every value drops before the exit, so a private copy is removed.
    let code = run();
    std::process::exit(code);
}

fn run() -> i32 {
    let mut args: Vec<_> = std::env::args_os().collect();
    let with_fold = args.iter().any(|a| a == "--fold");
    args.retain(|a| a != "--fold");
    if args.len() != 3 {
        eprintln!("usage: bm-verify [--fold] <store file> <keys dir>");
        return 2;
    }
    let store_path = Path::new(&args[1]);
    let keys_dir = Path::new(&args[2]);
    let mut fields: Vec<String> = vec![format!("{}: {}", quote("sqlite"), quote(sqlite_version()))];
    let mut timings: Vec<String> = Vec::new();

    let t = Instant::now();
    let store = match StoreFile::open(store_path) {
        Ok(s) => s,
        Err(StoreError::Admission(r)) => {
            // The reason names schema objects, never a value.
            fields.push(format!(
                "\"admission\": {{\"refused\": true, \"why\": {}, \"timed_out\": {}, \"later\": {}}}",
                quote(&r.why),
                r.timed_out,
                r.later.is_some()
            ));
            timings.push(format!("\"open\": {}", ms(t)));
            fields.push(format!("\"ms\": {{{}}}", timings.join(", ")));
            println!("{{{}}}", fields.join(", "));
            return 1;
        }
        Err(e) => return fail(&mut fields, "open", &e.to_string()),
    };
    timings.push(format!("\"open\": {}", ms(t)));
    // Meta, the counts and the chain from one snapshot of the store.
    if let Err(e) = store.connection().execute_batch("BEGIN") {
        return fail(&mut fields, "begin", &e.to_string());
    }
    fields.push(format!("\"read_from_copy\": {}", store.reads_a_copy()));

    let t = Instant::now();
    let meta = match store.meta() {
        Ok(m) => m,
        Err(e) => return fail(&mut fields, "meta", &e.to_string()),
    };
    let opened = store
        .check_opened(&meta, VERSION)
        .map(|v| v.opened)
        .map_err(|e| e.error);
    timings.push(format!("\"version_contract\": {}", ms(t)));
    let opened = match opened {
        Ok(o) => {
            let outcome = if o.verdict.read_only.is_some() {
                "read_only"
            } else {
                "readable"
            };
            fields.push(format!(
                "\"version_contract\": {{\"outcome\": {}, \"revision\": {}, \"min_reader\": {}, \"min_writer\": {}}}",
                quote(outcome),
                o.verdict.revision,
                o.verdict.min_reader,
                o.verdict.min_writer
            ));
            o
        }
        Err(StoreError::Version(e)) => {
            fields.push(format!(
                "\"version_contract\": {{\"outcome\": \"refused\", \"key\": {}, \"needed\": {}}}",
                e.key.map_or("null".into(), quote),
                e.needed.map_or("null".into(), |n| n.to_string())
            ));
            println!("{{{}}}", fields.join(", "));
            return 1;
        }
        Err(e) => return fail(&mut fields, "version_contract", &e.to_string()),
    };

    let t = Instant::now();
    let names = match store.table_names() {
        Ok(n) => n,
        Err(e) => return fail(&mut fields, "tables", &e.to_string()),
    };
    let mut counts = Vec::new();
    for name in &names {
        match store.count(name) {
            Ok(n) => counts.push(format!("{}: {n}", quote(name))),
            Err(e) => counts.push(format!(
                "{}: {}",
                quote(name),
                quote(&format!("error: {e}"))
            )),
        }
    }
    timings.push(format!("\"table_counts\": {}", ms(t)));
    fields.push(format!("\"table_counts\": {{{}}}", counts.join(", ")));

    let t = Instant::now();
    let ring = match load_key_ring(keys_dir, &opened.store_id) {
        Ok(r) => r,
        Err(e) => return fail(&mut fields, "key_ring", &format!("{e:?}")),
    };
    timings.push(format!("\"key_ring\": {}", ms(t)));
    fields.push(format!(
        "\"key_ring\": {{\"current\": {}, \"retired\": {}, \"head_file\": {}, \"origin_file\": {}}}",
        ring.current.is_some(),
        ring.retired.len(),
        ring.head_file.is_some(),
        ring.origin_file.is_some()
    ));

    // The chain outside that transaction: on a rollback-journal file read
    // in place it is read in short batches, and loaded again (meta, the
    // ring) if a rotation or a writer moves the store on meanwhile.
    drop(ring);
    let _ = store.connection().execute_batch("COMMIT");
    let t = Instant::now();
    let mut load = |s: &StoreFile| -> Result<(KeyRing, String), StoreError> {
        let meta = s.meta()?;
        let get = |k: &str| {
            meta.iter()
                .find(|(key, _)| key == k)
                .map(|(_, v)| v.clone())
        };
        let id = get("store_id").ok_or_else(|| StoreError::Refused("no store_id".into()))?;
        let fp = get("key_fingerprint")
            .ok_or_else(|| StoreError::Refused("no key_fingerprint".into()))?;
        let ring = load_key_ring(keys_dir, &id)
            .map_err(|e| StoreError::Refused(format!("key ring: {e:?}")))?;
        Ok((ring, fp))
    };
    if with_fold {
        // The chain and the fold on one state of the log
        // (`log_verify_loading`).
        let merged = match store.log_verify_loading(&mut load) {
            Ok(m) => m,
            Err(fold::FoldError::Store(StoreError::KeptChanging)) => {
                fields.push(format!(
                    "\"log_verify\": {{\"status\": \"kept_changing\", \"detail\": {}}}",
                    quote(&StoreError::KeptChanging.to_string())
                ));
                println!("{{{}}}", fields.join(", "));
                return 3;
            }
            Err(e) => return fail(&mut fields, "log_verify", &e.to_string()),
        };
        timings.push(format!("\"log_verify\": {}", ms(t)));
        // The longest the read lock was held at once: a batch, or the
        // final snapshot.
        timings.push(format!(
            "\"longest_hold\": {:.3}",
            store.longest_verify_hold().as_secs_f64() * 1000.0
        ));
        fields.push(format!("\"log_verify\": {}", log_verify_json(&merged)));
        fields.push(format!("\"ms\": {{{}}}", timings.join(", ")));
        drop(store);
        println!("{{{}}}", fields.join(", "));
        return i32::from(merged.status != "ok");
    }
    let report = match store.verify_loading(None, &mut load) {
        Ok(r) => r,
        Err(VerifyError::Py(e)) => return fail(&mut fields, "verify_chain", e.name()),
        Err(VerifyError::Source(StoreError::KeptChanging)) => {
            // Neither ok (0) nor a log that does not verify (1), nor a
            // failure (2): the store moved on under every try.
            fields.push(format!(
                "\"chain\": {{\"status\": \"kept_changing\", \"detail\": {}}}",
                quote(&StoreError::KeptChanging.to_string())
            ));
            println!("{{{}}}", fields.join(", "));
            return 3;
        }
        Err(VerifyError::Source(e)) => return fail(&mut fields, "verify_chain", &e.to_string()),
    };
    let verify_ms = t.elapsed().as_secs_f64() * 1000.0;
    timings.push(format!("\"verify_chain\": {verify_ms:.3}"));
    let rows_per_second = if verify_ms > 0.0 {
        report.rows as f64 / (verify_ms / 1000.0)
    } else {
        0.0
    };
    fields.push(format!(
        "\"chain\": {{\"status\": {}, \"rows\": {}, \"segments\": [{}], \"problems\": [{}], \"head\": {}, \"origin\": {}}}",
        quote(report.status),
        report.rows,
        report.segments.iter().map(dict).collect::<Vec<_>>().join(", "),
        report.problems.iter().map(dict).collect::<Vec<_>>().join(", "),
        dict(&report.head),
        dict(&report.origin)
    ));
    fields.push(format!("\"verify_rows_per_second\": {rows_per_second:.0}"));
    fields.push(format!("\"ms\": {{{}}}", timings.join(", ")));
    drop(store);
    println!("{{{}}}", fields.join(", "));
    i32::from(report.status != "ok")
}

fn cell(c: &Cell) -> String {
    match c {
        Cell::Null => "null".into(),
        Cell::Int(i) => i.to_string(),
        Cell::Real(f) => format!("{{\"float\": {}}}", quote(&format!("{:016x}", f.to_bits()))),
        Cell::Text(t) => quote(t),
        Cell::Blob(b) => format!(
            "{{\"blob\": \"{}\"}}",
            b.iter().map(|x| format!("{x:02x}")).collect::<String>()
        ),
    }
}

fn label(l: &Label) -> String {
    match l {
        Label::One(c) => cell(c),
        Label::Many(cs) => format!("[{}]", cs.iter().map(cell).collect::<Vec<_>>().join(", ")),
    }
}

fn labels(ls: &[Label]) -> String {
    format!("[{}]", ls.iter().map(label).collect::<Vec<_>>().join(", "))
}

fn tables(ts: &[TableFold]) -> String {
    let parts: Vec<String> = ts
        .iter()
        .map(|t| {
            format!(
                "{}: {{\"unaccounted\": {}, \"missing\": {}, \"rows\": {}}}",
                quote(t.table),
                labels(&t.unaccounted),
                labels(&t.missing),
                t.rows
            )
        })
        .collect();
    format!("{{{}}}", parts.join(", "))
}

fn log_verify_json(r: &fold::LogVerify) -> String {
    let problems: Vec<String> = r
        .problems
        .iter()
        .map(|p| match p {
            Problem::Chain(d) => dict(d),
            Problem::Payload(p) => format!(
                "{{\"seq\": {}, \"kind\": {}, \"problem\": \"payload_invalid\", \"detail\": {}}}",
                p.seq,
                quote(&p.kind),
                quote(&p.detail)
            ),
        })
        .collect();
    format!(
        "{{\"status\": {}, \"store_id\": {}, \"key_fingerprint\": {}, \"rows\": {}, \"segments\": [{}], \"head\": {}, \"origin\": {}, \"problems\": [{}], \"fold\": {{\"status\": {}, \"tables\": {}, \"membership\": {}}}, \"unaccounted_ids\": {}}}",
        quote(r.status),
        quote(&r.store_id),
        quote(&r.key_fingerprint),
        r.chain.rows,
        r.chain.segments.iter().map(dict).collect::<Vec<_>>().join(", "),
        dict(&r.chain.head),
        dict(&r.chain.origin),
        problems.join(", "),
        quote(r.fold.status),
        tables(&r.fold.tables),
        tables(&r.fold.membership),
        labels(r.unaccounted_ids())
    )
}
