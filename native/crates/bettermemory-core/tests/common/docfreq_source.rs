//! A stand-in for the store's side of the document frequency memo
//! (`store::docfreq::DocFreqSource`), as `gen_store_docfreq.py`'s `_Fake`
//! is the reference's: an in-memory SQLite database with the tables the
//! memo's reads name, the reference's statements, and its
//! `_advanced_inputs` written out here until the store's own implementation
//! takes its place. Every call to it is traced as the generator traces
//! the reference's.

use std::cell::RefCell;
use std::collections::HashMap;

use bettermemory_core::store::docfreq::{
    load_scopes, row_mask, AdmissionRow, DocActor, DocFreqError, DocFreqInputs, DocFreqSource,
    Group, GroupKey, Head, Mask, PyInt,
};
use rusqlite::{params, Connection};
use serde_json::Value;

const HEAD_KINDS: [&str; 5] = [
    "memory_put",
    "memory_delete",
    "home_put",
    "home_delete",
    "index_rebuilt",
];
const ADVANCE_ROWS: usize = 256;
const PROVENANCE_BATCH: usize = 500;

pub struct Double {
    pub number: usize,
    pub conn: Connection,
    pub digest: bool,
    pub tx: bool,
    pub trace: RefCell<Vec<String>>,
    pub hook: RefCell<Option<Value>>,
}

fn sql(e: rusqlite::Error) -> DocFreqError {
    DocFreqError::Source(e.to_string())
}

fn text(v: &Value) -> Option<String> {
    v.as_str().map(str::to_owned)
}

impl Double {
    pub fn new(number: usize, schema: &[Value]) -> Double {
        let conn = Connection::open_in_memory().unwrap();
        for statement in schema {
            conn.execute_batch(statement.as_str().unwrap()).unwrap();
        }
        Double {
            number,
            conn,
            digest: true,
            tx: false,
            trace: RefCell::new(Vec::new()),
            hook: RefCell::new(None),
        }
    }

    fn log(&self, kind: &str, payload: &Value) {
        let seq: i64 = self
            .conn
            .query_row("SELECT COALESCE(MAX(seq), 0) + 1 FROM log", [], |r| {
                r.get(0)
            })
            .unwrap();
        self.conn
            .execute(
                "INSERT INTO log(seq, kind, payload, mac) VALUES (?, ?, ?, ?)",
                params![seq, kind, payload.to_string(), format!("m{seq}")],
            )
            .unwrap();
    }

    fn rid(&self, id: &str) -> Option<i64> {
        self.conn
            .query_row("SELECT rowid FROM memories WHERE id = ?", [id], |r| {
                r.get(0)
            })
            .ok()
    }

    /// `_Case.put`: `scopes` a list (stored as `json.dumps`) or a text.
    pub fn put(
        &self,
        n: i64,
        body: &str,
        scopes: &Value,
        home: &Value,
        client: &Value,
        model: &Value,
    ) {
        let id = format!("r{n}");
        let (stored, words) = match scopes {
            Value::Array(items) => (
                python_dumps(scopes),
                items
                    .iter()
                    .filter_map(|x| x.as_str())
                    .collect::<Vec<_>>()
                    .join(" "),
            ),
            other => (
                other.as_str().unwrap().to_owned(),
                other.as_str().unwrap().to_owned(),
            ),
        };
        let (client, model) = (text(client), text(model));
        let c = &self.conn;
        let rid = match self.rid(&id) {
            None => {
                c.execute(
                    "INSERT INTO memories(id, scopes_json, actor_client, actor_model) VALUES (?, ?, ?, ?)",
                    params![id, stored, client, model],
                )
                .unwrap();
                self.rid(&id).unwrap()
            }
            Some(rid) => {
                c.execute(
                    "UPDATE memories SET scopes_json = ?, actor_client = ?, actor_model = ? WHERE id = ?",
                    params![stored, client, model, id],
                )
                .unwrap();
                rid
            }
        };
        c.execute("DELETE FROM memories_fts WHERE rowid = ?", [rid])
            .unwrap();
        c.execute(
            "INSERT INTO memories_fts(rowid, body_fts, scopes_fts) VALUES (?, ?, ?)",
            params![rid, body, words],
        )
        .unwrap();
        match text(home) {
            None => {
                c.execute("DELETE FROM homes WHERE record_id = ?", [&id])
                    .unwrap();
            }
            Some(p) => {
                c.execute(
                    "INSERT OR REPLACE INTO homes(record_id, project) VALUES (?, ?)",
                    params![id, p],
                )
                .unwrap();
            }
        }
        c.execute(
            "INSERT OR REPLACE INTO digest_records(rid, id, scopes, client, model, dl, removed) VALUES (?, ?, ?, ?, ?, ?, 0)",
            params![rid, id, stored, client, model, body.split_whitespace().count() as i64],
        )
        .unwrap();
        self.log("memory_put", &serde_json::json!({"memory": {"id": id}}));
    }

    /// One operation of the case on this store.
    pub fn apply(&mut self, op: &[Value]) {
        let c = &self.conn;
        match op[0].as_str().unwrap() {
            "put" => self.put(
                op[2].as_i64().unwrap(),
                op[3].as_str().unwrap(),
                &op[4],
                &op[5],
                &op[6],
                &op[7],
            ),
            "bulk" => {
                let first = op[2].as_i64().unwrap();
                for n in first..first + op[3].as_i64().unwrap() {
                    self.put(
                        n,
                        op[4].as_str().unwrap(),
                        &op[5],
                        &Value::Null,
                        &Value::Null,
                        &Value::Null,
                    );
                }
            }
            "home" => {
                let id = format!("r{}", op[2]);
                c.execute(
                    "INSERT OR REPLACE INTO homes(record_id, project) VALUES (?, ?)",
                    params![id, op[3].as_str().unwrap()],
                )
                .unwrap();
                self.log("home_put", &serde_json::json!({"home": {"record_id": id}}));
            }
            "unhome" => {
                let id = format!("r{}", op[2]);
                c.execute("DELETE FROM homes WHERE record_id = ?", [&id])
                    .unwrap();
                self.log("home_delete", &serde_json::json!({"record_id": id}));
            }
            "del" => {
                let id = format!("r{}", op[2]);
                let rid = self.rid(&id).unwrap();
                c.execute("DELETE FROM memories WHERE rowid = ?", [rid])
                    .unwrap();
                c.execute("DELETE FROM memories_fts WHERE rowid = ?", [rid])
                    .unwrap();
                c.execute("UPDATE digest_records SET removed = 1 WHERE rid = ?", [rid])
                    .unwrap();
                self.log("memory_delete", &serde_json::json!({"id": id}));
            }
            "log" => self.log(op[2].as_str().unwrap(), &serde_json::json!({})),
            "remac" => {
                c.execute(
                    "UPDATE log SET mac = 'x' WHERE seq = ?",
                    [op[2].as_i64().unwrap()],
                )
                .unwrap();
            }
            "digest" => self.digest = op[2].as_bool().unwrap(),
            "tx" => self.tx = op[2].as_bool().unwrap(),
            other => panic!("unknown op {other}"),
        }
    }

    fn rowids(
        &self,
        sql_text: &str,
        args: &[&dyn rusqlite::ToSql],
    ) -> Result<Vec<i64>, DocFreqError> {
        let mut stmt = self.conn.prepare(sql_text).map_err(sql)?;
        let rows = stmt.query_map(args, |r| r.get::<_, i64>(0)).map_err(sql)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(sql)
    }

    fn advance(
        &self,
        entry: &DocFreqInputs,
        head: &Head,
    ) -> Result<Option<DocFreqInputs>, DocFreqError> {
        let (Some(lengths), Some(group_of), Some(length_of)) =
            (&entry.lengths, &entry.group_of, &entry.length_of)
        else {
            return Ok(None);
        };
        let (seq, mac) = &entry.head;
        if head.0 <= *seq {
            return Ok(None);
        }
        let at: Option<String> = self
            .conn
            .query_row("SELECT mac FROM log WHERE seq = ?", [seq], |r| r.get(0))
            .ok();
        if at.as_deref() != Some(mac.as_str()) {
            return Ok(None);
        }
        let mut ids: Vec<String> = Vec::new();
        let mut stmt = self
            .conn
            .prepare("SELECT kind, payload FROM log WHERE kind IN (?, ?, ?, ?, ?) AND seq > ? AND seq <= ? ORDER BY seq")
            .map_err(sql)?;
        let rows: Vec<(String, String)> = stmt
            .query_map(
                params![
                    HEAD_KINDS[0],
                    HEAD_KINDS[1],
                    HEAD_KINDS[2],
                    HEAD_KINDS[3],
                    HEAD_KINDS[4],
                    seq,
                    head.0
                ],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .map_err(sql)?
            .collect::<Result<_, _>>()
            .map_err(sql)?;
        for (kind, payload) in rows {
            let v: Value = serde_json::from_str(&payload).unwrap();
            let id = match kind.as_str() {
                "memory_put" => v["memory"]["id"].clone(),
                "home_put" => v["home"]["record_id"].clone(),
                "home_delete" => v["record_id"].clone(),
                _ => return Ok(None),
            };
            let id = id.as_str().unwrap().to_owned();
            if !ids.contains(&id) {
                ids.push(id);
            }
            if ids.len() + entry.changed.len() > ADVANCE_ROWS {
                return Ok(None);
            }
        }
        if !self.digest {
            return Ok(None);
        }
        let mut read: Vec<AdmissionRow> = Vec::new();
        for chunk in ids.chunks(PROVENANCE_BATCH) {
            let marks = vec!["?"; chunk.len()].join(",");
            let statement = format!(
                "SELECT d.rid, d.scopes, h.project, d.client, d.model, d.dl \
                 FROM digest_records d LEFT JOIN homes h ON h.record_id = d.id \
                 WHERE d.removed = 0 AND d.id IN ({marks})"
            );
            let mut stmt = self.conn.prepare(&statement).map_err(sql)?;
            let got = stmt
                .query_map(rusqlite::params_from_iter(chunk.iter()), admission_row)
                .map_err(sql)?;
            for row in got {
                read.push(row.map_err(sql)?);
            }
        }
        let mut index = entry.index.clone();
        let mut group_of = group_of.clone();
        let mut length_of = length_of.clone();
        let mut groups = entry.groups.clone();
        let mut lengths = lengths.clone();
        let mut keys = entry.keys.clone();
        let mut number_of: HashMap<GroupKey, usize> = keys
            .iter()
            .enumerate()
            .map(|(n, k)| (k.clone(), n))
            .collect();
        let version = entry.version + 1;
        let mut changed = entry.changed.clone();
        for row in read {
            let scopes = load_scopes(&row.scopes_text)?;
            let position = match index.get(&row.rid) {
                None => {
                    if scopes.is_none() {
                        continue;
                    }
                    let position = index.len();
                    index.insert(row.rid.clone(), position);
                    group_of.push(-1);
                    length_of.push(0);
                    position
                }
                Some(&position) => {
                    let was = group_of[position];
                    if was >= 0 {
                        groups[was as usize].mask.clear(position);
                        lengths[was as usize] -= i128::from(length_of[position]);
                    }
                    group_of[position] = -1;
                    length_of[position] = 0;
                    position
                }
            };
            changed.push((version, row.rid.clone(), position));
            let Some(scopes) = scopes else { continue };
            let key: GroupKey = (
                row.scopes_text.clone(),
                row.home_project.clone(),
                row.client.clone(),
                row.model.clone(),
            );
            let number = match number_of.get(&key) {
                Some(&n) => n,
                None => {
                    let n = groups.len();
                    number_of.insert(key.clone(), n);
                    keys.push(key);
                    groups.push(Group {
                        scopes,
                        home_project: row.home_project.clone(),
                        actor: DocActor::of(&row.client, &row.model),
                        mask: Mask::default(),
                    });
                    lengths.push(0);
                    n
                }
            };
            groups[number].mask.set(position);
            lengths[number] += i128::from(row.length);
            group_of[position] = number as i32;
            length_of[position] = row.length;
        }
        Ok(Some(DocFreqInputs {
            head: head.clone(),
            groups,
            index,
            lengths: Some(lengths),
            keys,
            group_of: Some(group_of),
            length_of: Some(length_of),
            version,
            changed,
        }))
    }
}

fn admission_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<AdmissionRow> {
    let scopes: Option<String> = r.get(1)?;
    Ok(AdmissionRow {
        rid: PyInt::Small(r.get(0)?),
        scopes_text: scopes.unwrap_or_else(|| "None".into()),
        home_project: r.get(2)?,
        client: r.get(3)?,
        model: r.get(4)?,
        length: r.get(5)?,
    })
}

/// `json.dumps` of a JSON list of strings, ints and nulls.
pub fn python_dumps(v: &Value) -> String {
    match v {
        Value::Array(items) => format!(
            "[{}]",
            items
                .iter()
                .map(python_dumps)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        other => other.to_string(),
    }
}

impl DocFreqSource for Double {
    fn memory_head(&self) -> Result<Head, DocFreqError> {
        self.trace.borrow_mut().push("head".into());
        let row = self.conn.query_row(
            "SELECT seq, mac FROM log WHERE seq = (SELECT MAX(s) FROM (\
             SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_put' \
             UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_delete' \
             UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_put' \
             UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_delete' \
             UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'index_rebuilt'))",
            [],
            |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)),
        );
        match row {
            Ok(head) => Ok(head),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok((0, String::new())),
            Err(e) => Err(sql(e)),
        }
    }

    fn store_key(&self) -> (String, String) {
        (
            format!("/stores/s{}", self.number),
            format!("id{}", self.number),
        )
    }

    fn in_transaction(&self) -> bool {
        self.tx
    }

    fn admission_rows(&self) -> Result<(Vec<AdmissionRow>, bool), DocFreqError> {
        let statement = if self.digest {
            "SELECT d.rid, d.scopes, h.project, d.client, d.model, d.dl \
             FROM digest_records d LEFT JOIN homes h ON h.record_id = d.id \
             WHERE d.removed = 0 ORDER BY d.rid"
        } else {
            "SELECT m.rowid AS rid, m.scopes_json AS scopes_json, h.project AS home_project, \
             m.actor_client AS actor_client, m.actor_model AS actor_model, 0 \
             FROM memories m LEFT JOIN homes h ON h.record_id = m.id"
        };
        let mut stmt = self.conn.prepare(statement).map_err(sql)?;
        let rows = stmt
            .query_map([], admission_row)
            .map_err(sql)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(sql)?;
        Ok((rows, self.digest))
    }

    fn read_inputs(&self, head: &Head) -> Result<DocFreqInputs, DocFreqError> {
        self.trace.borrow_mut().push("read".into());
        let (rows, from_digest) = self.admission_rows()?;
        DocFreqInputs::from_rows(head.clone(), &rows, from_digest)
    }

    fn advanced_inputs(
        &self,
        entry: &DocFreqInputs,
        head: &Head,
    ) -> Result<Option<DocFreqInputs>, DocFreqError> {
        let out = self.advance(entry, head)?;
        self.trace
            .borrow_mut()
            .push(if out.is_some() { "adv:ok" } else { "adv:none" }.into());
        Ok(out)
    }

    fn term_masks(&self, term: &str, inputs: &DocFreqInputs) -> Result<(Mask, Mask), DocFreqError> {
        self.trace.borrow_mut().push(format!("tm:{term}"));
        let hook = self.hook.borrow_mut().take();
        if let Some(op) = hook {
            // The other connection's write: a put on this store.
            let op = op.as_array().unwrap().clone();
            self.put(
                op[2].as_i64().unwrap(),
                op[3].as_str().unwrap(),
                &op[4],
                &op[5],
                &op[6],
                &op[7],
            );
        }
        let quoted = bettermemory_core::store::docfreq::quoted(term);
        let matching = |expr: String| -> Result<Mask, DocFreqError> {
            let found = self.rowids(
                "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?",
                &[&expr],
            )?;
            let positions: Vec<usize> = found
                .iter()
                .filter_map(|r| inputs.index.get(&PyInt::Small(*r)).copied())
                .collect();
            row_mask(&positions, inputs.index.len())
        };
        let body = matching(format!("body_fts : {quoted}"))?;
        let mut either = body.clone();
        either.or_with(&matching(format!("scopes_fts : {quoted}"))?);
        Ok((body, either))
    }

    fn fts_rowids(&self, expr: &str, low: i64, limit: usize) -> Result<Vec<i64>, DocFreqError> {
        self.trace.borrow_mut().push(format!("fts:{expr}:{low}"));
        self.rowids(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? AND rowid >= ? ORDER BY rowid DESC LIMIT ?",
            &[&expr, &low, &(limit as i64)],
        )
    }
}
