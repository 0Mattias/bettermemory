//! What the record writer tests share: `store_records.jsonl`'s cases and
//! one writer call run natively as `gen_store_records._call` runs it.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::{Memory, Tombstone};
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::home::{truthy, Answer, Home};
use bettermemory_core::store::open::WritableStore;
use bettermemory_core::store::writers::{
    Put, PutTombstone, RecordWriteError, Relink, Trim, Update, Verify, WriteArgs,
};
use serde_json::Value;

use super::{jsonl, written};

/// The header and the cases of `store_records.jsonl`.
pub fn cases() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_records.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// A JSON value as the reference's `json.loads` reads it.
pub fn py(value: &Value) -> PyValue {
    written::payload(&serde_json::to_string(value).expect("text"))
}

/// The refusal as `gen_store_write._refusal` records it.
pub fn refusal(e: &RecordWriteError) -> Value {
    serde_json::json!({
        "class": e.class(),
        "text": e.text().map(|t| json::utf8_text(&t)),
    })
}

/// One call: the returned value's canonical payload text (None for
/// `delete_tombstone`), or the refusal. Only the transaction's reads and
/// writes happen inside; the caller compares.
pub fn call(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    call: &Value,
) -> Result<Option<String>, RecordWriteError> {
    let session = call["session"].as_str();
    match call["call"].as_str().expect("call") {
        "put" => {
            let memory = Memory::from_payload(&py(&call["memory"])).expect("a valid record");
            let provenance = call.get("provenance").map(py);
            let home = call.get("home").map(|_| fixed_home());
            let standing = call.get("standing").map(|_| fixed_answer());
            let put = Put {
                provenance: provenance.as_ref(),
                filename: call.get("filename").map(|f| f.as_str().expect("text")),
                session,
                home: home.as_ref(),
                standing: standing.as_ref(),
            };
            let done = store.put_memory(provider, &memory, put)?;
            Ok(Some(done.value.payload_text().expect("payload")))
        }
        "tombstone" => {
            let now = call["now"].as_str().map(written::instant);
            let done = store.tombstone(
                provider,
                call["id"].as_str().expect("id"),
                call["reason"].as_str().expect("reason"),
                session,
                now,
            )?;
            Ok(Some(done.value.payload_text().expect("payload")))
        }
        "delete_tombstone" => {
            store.delete_tombstone(provider, call["id"].as_str().expect("id"), session)?;
            Ok(None)
        }
        other => panic!("an unknown call {other}"),
    }
}

/// The fixture's `home`: `RecordHome("global", None, "fallback")`.
pub fn fixed_home() -> Home {
    Home {
        project: PyValue::str("global"),
        checkout: PyValue::None,
        rule: PyValue::str("fallback"),
    }
}

/// The fixture's `standing`: `StandingAnswer(0.5, "explicit", "test")`.
pub fn fixed_answer() -> Answer {
    Answer {
        probability: PyValue::Float(0.5),
        source: PyValue::str("explicit"),
        answered_by: PyValue::str("test"),
    }
}

/// The header and the cases of `store_records_b.jsonl`.
pub fn cases_b() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_records_b.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

fn answer_of(v: &Value) -> Option<Answer> {
    let a = v.as_array()?;
    Some(Answer {
        probability: py(&a[0]),
        source: py(&a[1]),
        answered_by: py(&a[2]),
    })
}

fn home_of(v: &Value) -> Option<Home> {
    let h = v.as_array()?;
    Some(Home {
        project: py(&h[0]),
        checkout: py(&h[1]),
        rule: py(&h[2]),
    })
}

/// One call of `store_records_b.jsonl` (`gen_store_records_b._run`).
pub fn call_b(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    call: &Value,
) -> Result<Option<String>, RecordWriteError> {
    let session = call["session"].as_str();
    let done = match call["call"].as_str().expect("call") {
        "put" => {
            let memory = Memory::from_payload(&py(&call["memory"])).expect("a valid record");
            let put = Put {
                session,
                ..Put::default()
            };
            store.put_memory(provider, &memory, put)?
        }
        "tombstone" => {
            store.tombstone(
                provider,
                call["id"].as_str().expect("id"),
                "removed",
                session,
                None,
            )?;
            return Ok(None);
        }
        "write" => {
            let a = &call["args"];
            let get = |k: &str| a.get(k).map(py);
            let (content, scopes) = (py(&a["content"]), py(&a["scopes"]));
            let (confidence, source, origin) = (get("confidence"), get("source"), get("origin"));
            let (category, claims, links, actor) =
                (get("category"), get("claims"), get("links"), get("actor"));
            let home = home_of(&call["home"]);
            let standing = answer_of(&call["standing"]);
            let args = WriteArgs {
                content: &content,
                scopes: &scopes,
                confidence: confidence.as_ref(),
                source: source.as_ref(),
                origin: origin.as_ref(),
                category: category.as_ref(),
                claims: claims.as_ref(),
                links: links.as_ref(),
                actor: actor.as_ref(),
                session,
                home: home.as_ref(),
                standing: standing.as_ref(),
            };
            store.write(provider, args)?
        }
        "update" => {
            let memory = Memory::from_payload(&py(&call["memory"])).expect("a valid record");
            let standing = answer_of(&call["standing"]);
            let update = Update {
                force: call["force"].as_bool().expect("force"),
                preserve_verification: call["preserve_verification"].as_bool().expect("p"),
                session,
                standing: standing.as_ref(),
            };
            store.update(provider, &memory, update)?
        }
        other => panic!("an unknown call {other}"),
    };
    Ok(Some(done.value.payload_text().expect("payload")))
}

/// A provider whose ids are `01K` numbered from `next`, everything else
/// `inner`'s: for tests that write several records through `write`.
pub struct Minting<P> {
    pub inner: P,
    pub next: u64,
}

impl<P: Provider> Provider for Minting<P> {
    fn store_id(&mut self) -> std::io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> std::io::Result<bettermemory_core::log::Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> std::io::Result<String> {
        self.inner.membership_store_id()
    }
    fn now(&mut self) -> std::io::Result<bettermemory_core::datetime::DateTime> {
        self.inner.now()
    }
    fn wall_now(&mut self) -> std::io::Result<bettermemory_core::datetime::DateTime> {
        self.inner.wall_now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> std::io::Result<String> {
        self.next += 1;
        Ok(written::ulid(self.next, "01K"))
    }
}

/// The header and the cases of `store_records_c.jsonl`.
pub fn cases_c() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_records_c.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// One call of `store_records_c.jsonl` (`gen_store_records_c._run`):
/// `verify` and `corroborate`, the rest as [`call_b`].
pub fn call_c(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    call: &Value,
) -> Result<Option<String>, RecordWriteError> {
    let session = call["session"].as_str();
    let id = call["id"].as_str().unwrap_or_default();
    match call["call"].as_str().expect("call") {
        "verify" => {
            let kw = &call["kw"];
            let get = |k: &str| kw.get(k).map(py);
            let values: Vec<Option<PyValue>> = [
                "verified_paths",
                "verified_commits",
                "verified_versions",
                "verified_absent_paths",
                "verified_path_targets",
                "verified_absent_path_targets",
                "claims",
                "verified_head",
                "expected_last_verified_at",
                "expected_updated",
            ]
            .iter()
            .map(|k| get(k))
            .collect();
            let v = |i: usize| values[i].as_ref();
            let verify = Verify {
                verified_paths: v(0),
                verified_commits: v(1),
                verified_versions: v(2),
                verified_absent_paths: v(3),
                verified_path_targets: v(4),
                verified_absent_path_targets: v(5),
                claims: v(6),
                verified_head: v(7),
                expected_last_verified_at: v(8),
                expected_updated: v(9),
                check_expected: kw
                    .get("check_expected")
                    .map(|v| truthy(&py(v)))
                    .unwrap_or(false),
                session,
            };
            let done = store.mark_verified(provider, id, verify)?;
            Ok(Some(done.value.payload_text().expect("payload")))
        }
        "corroborate" => {
            let done = store.record_corroboration(provider, id, session)?;
            Ok(done.value.map(|m| m.payload_text().expect("payload")))
        }
        _ => call_b(store, provider, call),
    }
}

/// The header and the cases of `store_records_d.jsonl`.
pub fn cases_d() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_records_d.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// What a restore's relink reads that the reference keeps on its `Store`:
/// the opener's `strict_provenance` and the ids its last refold found.
#[derive(Clone, Debug, Default)]
pub struct Reads {
    pub strict: bool,
    pub memo: Vec<String>,
}

impl Reads {
    pub fn relink(&self) -> Relink<'_> {
        Relink {
            strict: self.strict,
            unaccounted: &self.memo,
        }
    }
}

/// One call of `store_records_d.jsonl` (`gen_store_records_d._run`):
/// `sql` (a hand edit, committed), `put_tombstone`, `restore` and
/// `restore_trimmed`, the rest as [`call_b`]; `reads` as the reference's
/// store holds them.
pub fn call_d(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    call: &Value,
    reads: &Reads,
) -> Result<Option<String>, RecordWriteError> {
    let session = call["session"].as_str();
    let id = call["id"].as_str().unwrap_or_default();
    match call["call"].as_str().expect("call") {
        "sql" => {
            // One statement, or several, each given the id.
            let statements: Vec<&str> = match &call["sql"] {
                Value::Array(list) => list.iter().map(|v| v.as_str().expect("sql")).collect(),
                one => vec![one.as_str().expect("sql")],
            };
            for statement in statements {
                store
                    .connection()
                    .execute(statement, [id])
                    .expect("the hand edit");
            }
            Ok(None)
        }
        "put_tombstone" => {
            let dead = Tombstone::from_payload(&py(&call["dead"])).expect("a valid tombstone");
            let kw = &call["kw"];
            let get = |k: &str| kw.get(k).map(py);
            let (provenance, links, corroborations) =
                (get("provenance"), get("links"), get("corroborations"));
            let filename = kw.get("filename").and_then(Value::as_str);
            let last = kw
                .get("last_corroborated")
                .and_then(Value::as_str)
                .map(|t| {
                    DateTime::from_isoformat(&bettermemory_core::unicode::cps(t)).expect("a time")
                });
            let args = PutTombstone {
                provenance: provenance.as_ref(),
                filename,
                links: links.as_ref(),
                corroborations: corroborations.as_ref(),
                last_corroborated: last,
                session,
            };
            let done = store.put_tombstone(provider, &dead, args)?;
            Ok(Some(done.value.payload_text().expect("payload")))
        }
        "restore" => {
            let done = store.restore(provider, id, session, reads.relink())?;
            Ok(Some(done.value.payload_text().expect("payload")))
        }
        "restore_trimmed" => {
            let kw = &call["kw"];
            let get = |k: &str| kw.get(k).map(py);
            let values: Vec<Option<PyValue>> = [
                "drop_claims",
                "drop_verified_paths",
                "clear_verification",
                "drop_verified_head",
            ]
            .iter()
            .map(|k| get(k))
            .collect();
            let trim = Trim {
                drop_claims: values[0].as_ref(),
                drop_verified_paths: values[1].as_ref(),
                clear_verification: values[2].as_ref(),
                drop_verified_head: values[3].as_ref(),
                session,
                relink: reads.relink(),
            };
            let done = store.restore_trimmed(provider, id, trim)?;
            Ok(Some(done.value.payload_text().expect("payload")))
        }
        _ => call_b(store, provider, call),
    }
}

/// The header and the cases of `store_records_e.jsonl`.
pub fn cases_e() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_records_e.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// One step of `store_records_e.jsonl` (`gen_store_records_e._run_all`):
/// `rekey`, `drop`, `drop_k0` and `reopen` replay exactly what the
/// reference wrote (its row, meta, key files); `refold` is native's own,
/// its unaccounted ids kept in `reads` as the reference keeps them (and
/// returned, for the comparison); the rest as [`call_d`].
pub fn call_e(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    call: &Value,
    reads: &mut Reads,
) -> Result<Option<Value>, RecordWriteError> {
    let keys = store.keys_dir().to_path_buf();
    let file = store.path().to_path_buf();
    match call["call"].as_str().expect("call") {
        "rekey" => {
            let row = call["row"].as_array().expect("row");
            let conn = store.connection();
            conn.execute(
                "INSERT INTO log (seq, ts, session, kind, payload, prev_mac, mac) \
                 VALUES (?, ?, ?, ?, ?, ?, ?)",
                rusqlite::params![
                    row[0].as_i64(),
                    row[1].as_str(),
                    row[2].as_str(),
                    row[3].as_str(),
                    row[4].as_str(),
                    row[5].as_str(),
                    row[6].as_str()
                ],
            )
            .expect("the rekey row");
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
                [call["meta"].as_str().expect("meta")],
            )
            .expect("meta");
            for f in call["files"].as_array().expect("files") {
                let bytes = (0..f[1].as_str().expect("hex").len() / 2)
                    .map(|i| {
                        u8::from_str_radix(&f[1].as_str().expect("hex")[2 * i..2 * i + 2], 16)
                            .expect("hex")
                    })
                    .collect::<Vec<u8>>();
                std::fs::write(keys.join(f[0].as_str().expect("name")), bytes).expect("key");
            }
            Ok(Some(Value::Null))
        }
        "drop" | "drop_k0" => match call.get("file").and_then(Value::as_str) {
            Some(name) => {
                std::fs::remove_file(keys.join(name)).expect("drop");
                Ok(Some(Value::Null))
            }
            // The reference's unlink refused (it records no file): the
            // same ring here has no such file either.
            None => Err(RecordWriteError::Write(
                bettermemory_core::store::write::WriteError::Refused {
                    class: "FileNotFoundError",
                    text: None,
                },
            )),
        },
        "reopen" => {
            // `Store.close()` then `Store.open()`: a new store, whose
            // `_unaccounted_memory_ids` starts empty.
            reads.strict = call["strict"].as_bool().expect("strict");
            reads.memo.clear();
            let again = bettermemory_core::store::open::open_for_writing(
                &file,
                &keys,
                &super::opened::engine_provider(),
            )
            .expect("reopened");
            let old = std::mem::replace(store, again);
            old.close().expect("closed");
            Ok(Some(Value::Null))
        }
        "later_field" => {
            for e in call["edits"].as_array().expect("edits") {
                let params: Vec<rusqlite::types::Value> = e[1]
                    .as_array()
                    .expect("params")
                    .iter()
                    .map(|v| match v {
                        Value::Number(n) => {
                            rusqlite::types::Value::Integer(n.as_i64().expect("int"))
                        }
                        Value::String(t) => rusqlite::types::Value::Text(t.clone()),
                        other => panic!("a parameter {other}"),
                    })
                    .collect();
                store
                    .connection()
                    .execute(
                        e[0].as_str().expect("sql"),
                        rusqlite::params_from_iter(params),
                    )
                    .expect("the signed edit");
            }
            Ok(Some(Value::Null))
        }
        "refold" => {
            let read = bettermemory_core::store::StoreFile::open(&file).expect("read");
            let report = read.refold().expect("refold");
            let mut ids: Vec<String> = report
                .unaccounted_ids()
                .iter()
                .map(|l| match l {
                    bettermemory_core::store::fold::Label::One(
                        bettermemory_core::store::fold::Cell::Text(t),
                    ) => t.clone(),
                    other => panic!("an id {other:?}"),
                })
                .collect();
            ids.sort();
            reads.memo = ids.clone();
            Ok(Some(serde_json::json!(ids)))
        }
        _ => {
            call_d(store, provider, call, reads).map(|r| Some(r.map_or(Value::Null, Value::String)))
        }
    }
}
