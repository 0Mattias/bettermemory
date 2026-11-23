//! Build an ordinary signed two-row store through the production writer.
#![allow(dead_code)]
use bettermemory_core::log::{fingerprint, ring::KeyFiles, Key};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::{
    create::{create, System},
    open::open_for_writing,
    record_event::{record_event, EventCall, EventTransaction},
};
use bettermemory_core::unicode::cps;
use rusqlite::{
    hooks::{AuthAction, AuthContext, Authorization},
    Connection,
};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
};

pub struct Signed {
    pub path: PathBuf,
    pub ring: KeyFiles,
    pub current: Key,
    pub named: String,
    pub first_head: Vec<u8>,
}
impl Signed {
    pub fn new(root: &Path) -> Self {
        let path = root.join("memory.sqlite");
        let dir = root.join("keys");
        let mut clock = System {
            engine_version: "fixture".into(),
        };
        let created = create(&path, &dir, &mut clock).unwrap();
        let ring = KeyFiles::new(&dir, &PyValue::str(&created.store_id)).unwrap();
        let first_head = fs::read(ring.head_path()).unwrap();
        let mut write = open_for_writing(&path, &dir, &clock).unwrap();
        let call = EventCall {
            kind: &cps("search"),
            session: Some(&cps("ordinary")),
            best_effort: false,
            attempts: 1,
            deadline: None,
        };
        let fields = [(PyValue::str("query"), PyValue::str("sample"))];
        record_event(
            Ok(EventTransaction::Store(&mut write, &mut clock)),
            &call,
            &fields,
            None,
            &mut |_| Vec::new(),
            &mut Vec::new(),
        )
        .unwrap()
        .unwrap();
        write.close().unwrap();
        let current = ring.load_current().unwrap().unwrap();
        let named = fingerprint(current.bytes());
        Self {
            path,
            ring,
            current,
            named,
            first_head,
        }
    }
    pub fn copy_ring(&self, directory: &Path) -> KeyFiles {
        fs::create_dir_all(directory).unwrap();
        for entry in fs::read_dir(self.ring.keys_dir()).unwrap() {
            let e = entry.unwrap();
            fs::copy(e.path(), directory.join(e.file_name())).unwrap();
        }
        KeyFiles::new(directory, &PyValue::str(self.ring.store_id())).unwrap()
    }
}
pub fn selects(conn: &mut Connection) -> Arc<AtomicUsize> {
    let total = Arc::new(AtomicUsize::new(0));
    let count = total.clone();
    conn.authorizer(Some(move |ctx: AuthContext<'_>| {
        if matches!(ctx.action, AuthAction::Select) {
            count.fetch_add(1, Ordering::Relaxed);
        }
        Authorization::Allow
    }))
    .unwrap();
    total
}
