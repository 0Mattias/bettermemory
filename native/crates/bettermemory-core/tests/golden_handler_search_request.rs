//! The request values, setting reads and record identities observed by Python.
mod common;

use bettermemory_core::handlers::search_request::width::{request_width_and_mode, Width};
use bettermemory_core::handlers::search_request::{
    Hydrated, Hydrator, LoadMany, RecordId, TopExpansion,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use serde_json::Value;
use std::{borrow::Cow, cell::RefCell, collections::HashMap, sync::Arc};

fn str_value(v: &Value) -> PyStr {
    assert_eq!(v["t"], "str");
    common::text_field(v, "v")
}

fn width_and_mode(row: &Value) {
    let calls = RefCell::new(Vec::new());
    let bytes;
    let value;
    let width = match row["width"]["t"].as_str().unwrap() {
        t @ ("bytes" | "bytearray") => {
            bytes = common::unhex(row["width"]["hex"].as_str().unwrap());
            if t == "bytes" {
                Width::Bytes(&bytes)
            } else {
                Width::ByteArray(&bytes)
            }
        }
        _ => {
            value = common::neutral(&row["width"]);
            Width::Value(&value)
        }
    };
    let mode = common::neutral(&row["mode"]);
    let default_width = common::neutral(&row["defaults"][0]);
    let default_mode = common::neutral(&row["defaults"][1]);
    let got = request_width_and_mode(
        width,
        &mode,
        || {
            calls.borrow_mut().push("width");
            Ok(Width::Value(&default_width))
        },
        || {
            calls.borrow_mut().push("mode");
            Ok(&default_mode)
        },
    );
    assert_eq!(serde_json::json!(calls.into_inner()), row["calls"]);
    let expected = &row["result"];
    match got {
        Ok((width, mode)) => {
            let pair = expected["ok"].as_array().expect("expected a success");
            assert_eq!(width, pair[0].as_i64().unwrap());
            assert_eq!(mode, &PyValue::str(pair[1].as_str().unwrap()));
        }
        Err(e) => {
            assert_eq!(
                e.class,
                expected["error"].as_str().expect("expected an error")
            );
            assert_eq!(e.message, str_value(&expected["text"]));
        }
    }
}

#[derive(Debug)]
struct Item {
    id: PyStr,
    name: String,
}
impl RecordId<&'static str> for Item {
    fn record_id(&self) -> Result<Cow<'_, [u32]>, &'static str> {
        Ok(Cow::Borrowed(&self.id))
    }
}

struct Loader {
    items: HashMap<String, Arc<Item>>,
    replies: RefCell<Vec<String>>,
    calls: RefCell<Vec<Vec<PyStr>>>,
}

impl LoadMany<Item> for &Loader {
    type Error = &'static str;
    fn load_many(
        &mut self,
        ids: &[PyStr],
        emit: &mut dyn FnMut(Arc<Item>) -> Result<(), Self::Error>,
    ) -> Result<(), Self::Error> {
        self.calls.borrow_mut().push(ids.to_vec());
        for label in self.replies.borrow().iter() {
            if label == "raise" {
                return Err("loader stopped");
            }
            emit(Arc::clone(&self.items[label]))?;
        }
        Ok(())
    }
}

fn hydration(row: &Value) {
    let mut loader = Loader {
        items: HashMap::new(),
        replies: RefCell::default(),
        calls: RefCell::default(),
    };
    for pair in row["records"].as_array().unwrap() {
        let name = pair[0].as_str().unwrap().to_owned();
        loader.items.insert(
            name.clone(),
            Arc::new(Item {
                name,
                id: str_value(&pair[1]),
            }),
        );
    }
    let mut hydrator = Hydrator::new(&loader);
    for step in row["steps"].as_array().unwrap() {
        loader.calls.borrow_mut().clear();
        *loader.replies.borrow_mut() = step["replies"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect();
        let inputs: Vec<_> = step["inputs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| loader.items[v.as_str().unwrap()].as_ref())
            .collect();
        let rows = match step["action"].as_str().unwrap() {
            "replace" => hydrator.replaced(&inputs),
            "load" => hydrator.hydrate(&inputs),
            "new" => {
                hydrator = Hydrator::new(&loader);
                hydrator.hydrate(&inputs)
            }
            other => panic!("unknown action {other}"),
        };
        match rows {
            Ok(items) => {
                let expected = step["result"]["ok"].as_array().expect("result records");
                assert_eq!(items.len(), expected.len());
                for (returned, want) in items.iter().zip(expected) {
                    let got = match returned {
                        Hydrated::Original(item) => **item,
                        Hydrated::Full(item) => item.as_ref(),
                    };
                    assert!(
                        std::ptr::eq(got, loader.items[want.as_str().unwrap()].as_ref()),
                        "{}",
                        got.name
                    );
                }
            }
            Err(error) => assert_eq!(error, step["result"]["error"].as_str().unwrap()),
        }
        let requests: Vec<Vec<PyStr>> = step["requested"]
            .as_array()
            .unwrap()
            .iter()
            .map(|ids| ids.as_array().unwrap().iter().map(str_value).collect())
            .collect();
        assert_eq!(*loader.calls.borrow(), requests);
        let cache = step["cache"].as_array().unwrap();
        assert_eq!(hydrator.full.len(), cache.len());
        for ((key, item), expected) in hydrator.full.iter().zip(cache) {
            assert_eq!(key, &str_value(&expected[0]));
            assert!(Arc::ptr_eq(
                item,
                &loader.items[expected[1].as_str().unwrap()]
            ));
        }
    }
}

fn expansion(row: &Value) {
    let fields = &row["fields"];
    let text = |field: &Value| (field["t"] != "null").then(|| str_value(field));
    let integer = |field: &Value| field["v"].as_str().map(str::to_owned);
    let value = TopExpansion {
        expanded_id: text(&fields[0]),
        drift_missing: integer(&fields[1]).unwrap(),
        commit_drift_status: text(&fields[2]),
        commits_since_verify: integer(&fields[3]),
    };
    let event = &row["event"];
    assert_eq!(value.expanded_id, text(&event[0]));
    assert_eq!(Some(value.drift_missing), integer(&event[1]));
    assert_eq!(value.commit_drift_status, text(&event[2]));
    assert_eq!(value.commits_since_verify, integer(&event[3]));
}

#[test]
fn all_search_request_rows() {
    let rows = common::jsonl("handler_search_request.jsonl");
    for row in &rows {
        match row["kind"].as_str().unwrap() {
            "resolve" => width_and_mode(row),
            "hydrate" => hydration(row),
            "expand" => expansion(row),
            other => panic!("unknown row {other}"),
        }
    }
    assert_eq!(rows.len(), 109);
}

#[test]
fn a_missing_candidate_reads_its_id_again_for_the_load() {
    use bettermemory_core::unicode::cps;
    use std::cell::Cell;

    struct Candidate {
        next: Cell<usize>,
        ids: Vec<PyStr>,
    }
    impl RecordId<&'static str> for Candidate {
        fn record_id(&self) -> Result<Cow<'_, [u32]>, &'static str> {
            let n = self.next.get();
            self.next.set(n + 1);
            Ok(Cow::Borrowed(&self.ids[n]))
        }
    }

    let whole = Arc::new(Item {
        name: "full".into(),
        id: cps("returned"),
    });
    let loader = Loader {
        items: HashMap::from([("full".into(), Arc::clone(&whole))]),
        replies: RefCell::new(vec!["full".into()]),
        calls: RefCell::default(),
    };
    let candidate = Candidate {
        next: Cell::new(0),
        ids: ["checked", "requested", "returned", "returned", "returned"]
            .map(cps)
            .to_vec(),
    };
    let input = std::slice::from_ref(&candidate);
    let mut hydrator = Hydrator::new(&loader);
    for _ in 0..2 {
        let rows = hydrator.hydrate(input).unwrap();
        assert!(matches!(&rows[0],Hydrated::Full(got) if Arc::ptr_eq(got,&whole)));
    }
    assert_eq!(*loader.calls.borrow(), vec![vec![cps("requested")]]);
    assert_eq!(candidate.next.get(), 5);
}

#[test]
fn id_errors_stop_at_the_failing_read_and_keep_earlier_writes() {
    use bettermemory_core::unicode::cps;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Access {
        id: PyStr,
        reads: AtomicUsize,
        fail_on: usize,
        error: Arc<str>,
    }
    impl RecordId<Arc<str>> for Access {
        fn record_id(&self) -> Result<Cow<'_, [u32]>, Arc<str>> {
            let count = self.reads.fetch_add(1, Ordering::Relaxed) + 1;
            if count == self.fail_on {
                Err(self.error.clone())
            } else {
                Ok(Cow::Borrowed(&self.id))
            }
        }
    }
    struct Stream<'a>(&'a [Arc<Access>], &'a AtomicUsize);
    impl LoadMany<Access> for Stream<'_> {
        type Error = Arc<str>;
        fn load_many(
            &mut self,
            ids: &[PyStr],
            emit: &mut dyn FnMut(Arc<Access>) -> Result<(), Self::Error>,
        ) -> Result<(), Self::Error> {
            assert_eq!(ids, [cps("a")]);
            for row in self.0 {
                self.1.fetch_add(1, Ordering::Relaxed);
                emit(Arc::clone(row))?;
            }
            Ok(())
        }
    }

    for (replace_only, input_failure, second_row_failure, yielded, cached) in [
        (false, 1, 0, 0, 0),
        (false, 2, 0, 0, 0),
        (false, 0, 1, 2, 1),
        (false, 3, 0, 3, 3),
        (true, 1, 0, 0, 0),
    ] {
        let error: Arc<str> = Arc::from("id lookup failed");
        let access = |id: &str, fail_on| Access {
            id: cps(id),
            fail_on,
            reads: AtomicUsize::new(0),
            error: error.clone(),
        };
        let candidates = [access("a", input_failure)];
        let rows = [
            Arc::new(access("a", 0)),
            Arc::new(access("new", second_row_failure)),
            Arc::new(access("after", 0)),
        ];
        let advanced = AtomicUsize::new(0);
        let mut request = Hydrator::new(Stream(&rows, &advanced));
        let result = if replace_only {
            request.replaced(&candidates)
        } else {
            request.hydrate(&candidates)
        };
        let Err(observed) = result else {
            panic!("expected id failure")
        };
        assert!(Arc::ptr_eq(&observed, &error));
        assert_eq!(advanced.load(Ordering::Relaxed), yielded);
        assert_eq!(request.full.len(), cached);
        for ((id, row), expected) in request.full.iter().zip(&rows) {
            assert_eq!(id, &expected.id);
            assert!(Arc::ptr_eq(row, expected));
        }
    }
}
