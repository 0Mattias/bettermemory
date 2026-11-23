use std::rc::Rc;

use bettermemory_core::handlers::search_request::width::{
    request_width_and_mode, resolve_with, RequestError, RequestProtocol, Width,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

#[test]
fn bounds_and_literal_diagnostics_match_int_before_clamping() {
    let mode = PyValue::str("bm25");
    let width = |v| {
        request_width_and_mode(
            v,
            &mode,
            || panic!("width setting"),
            || panic!("mode setting"),
        )
        .map(|(w, selected)| {
            assert!(std::ptr::eq(selected, &mode));
            w
        })
    };
    let huge = PyValue::Int("1".repeat(12_000));
    let negative = PyValue::Int("-".to_owned() + &"7".repeat(12_000));
    let bad = PyValue::str("bad");
    assert_eq!(width(Width::Value(&huge)), Ok(50));
    assert_eq!(width(Width::Value(&negative)), Ok(1));
    assert_eq!(width(Width::Bytes(b" +5_0\t")), Ok(50));
    assert_eq!(width(Width::ByteArray(b" 4_3 ")), Ok(43));
    for (input, shown) in [
        (Width::Bytes(b"\x851"), "b'\\x851'"),
        (Width::ByteArray(b"1\0"), "b'1\\x00'"),
        (Width::Value(&bad), "'bad'"),
    ] {
        let error = width(input).unwrap_err();
        assert_eq!(error.class, "ValueError");
        assert_eq!(
            error.message,
            cps(&format!("invalid literal for int() with base 10: {shown}"))
        );
    }
    let mut digits = vec![b'3'; 4301];
    digits.extend_from_slice(b"\0\xff");
    let error = width(Width::Bytes(&digits)).unwrap_err();
    assert_eq!(error.message,cps("Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit"));
}

#[test]
fn the_mode_or_its_representation_can_raise_after_width_succeeds() {
    let width = PyValue::int(2);
    let mode = PyValue::List(vec![PyValue::Int("9".repeat(5000))]);
    let err =
        request_width_and_mode(Width::Value(&width), &mode, || panic!(), || panic!()).unwrap_err();
    assert_eq!(err.class, "ValueError");
    assert_eq!(err.message,cps("Exceeds the limit (4300 digits) for integer string conversion; use sys.set_int_max_str_digits() to increase the limit"));
    let path = PyValue::Path(cps("."));
    let err =
        request_width_and_mode(Width::Value(&width), &path, || panic!(), || panic!()).unwrap_err();
    assert_eq!(
        err.message,
        cps("unknown search mode PurePosixPath('.'); must be one of: keyword, bm25, hybrid")
    );
    let date = PyValue::DateTime(
        bettermemory_core::datetime::DateTime::from_isoformat(&cps("2024-01-02")).unwrap(),
    );
    let err =
        request_width_and_mode(Width::Value(&date), &path, || panic!(), || panic!()).unwrap_err();
    assert_eq!(err.class, "TypeError");
    assert_eq!(err.message, cps("int() argument must be a string, a bytes-like object or a real number, not 'datetime.datetime'"));
    let err =
        request_width_and_mode(Width::Value(&width), &date, || panic!(), || panic!()).unwrap_err();
    assert_eq!(err.message, cps("unknown search mode datetime.datetime(2024, 1, 2, 0, 0); must be one of: keyword, bm25, hybrid"));
}

#[test]
fn invalid_bytes_choose_quotes_from_the_first_200_bytes() {
    let mode = PyValue::str("bm25");
    for (position, quote) in [(199, '"'), (200, '\'')] {
        let mut raw = vec![b'1'; position];
        raw.push(b'\'');
        for width in [Width::Bytes(&raw), Width::ByteArray(&raw)] {
            let err = request_width_and_mode(width, &mode, || panic!(), || panic!()).unwrap_err();
            assert_eq!(err.class, "ValueError");
            let expected = format!(
                "invalid literal for int() with base 10: b{quote}{}",
                "1".repeat(198)
            );
            assert_eq!(err.message, cps(&expected));
        }
    }
}

#[derive(Debug)]
enum Exception {
    Source(Rc<()>),
    Own(RequestError),
}

impl From<RequestError> for Exception {
    fn from(value: RequestError) -> Self {
        Self::Own(value)
    }
}

struct Host {
    trace: Vec<&'static str>,
    stop: Option<&'static str>,
    failure: Rc<()>,
}

impl Host {
    fn new(stop: Option<&'static str>) -> Self {
        Self {
            trace: Vec::new(),
            stop,
            failure: Rc::new(()),
        }
    }
    fn visit(&mut self, name: &'static str) -> Result<(), Exception> {
        self.trace.push(name);
        if self.stop == Some(name) {
            Err(Exception::Source(self.failure.clone()))
        } else {
            Ok(())
        }
    }
}

impl RequestProtocol for Host {
    type Width = &'static str;
    type Integer = String;
    type Mode = Rc<str>;
    type Error = Exception;
    fn default_width(&mut self) -> Result<Self::Width, Exception> {
        self.visit("default width")?;
        Ok("index")
    }
    fn integer(&mut self, value: Self::Width) -> Result<String, Exception> {
        self.visit(value)?;
        Ok(if value == "index" {
            "39".into()
        } else {
            "2".repeat(5000)
        })
    }
    fn default_mode(&mut self) -> Result<Self::Mode, Exception> {
        self.visit("default mode")?;
        Ok(Rc::from(""))
    }
    fn hybrid(&self) -> Self::Mode {
        Rc::from("hybrid")
    }
    fn truth(&mut self, mode: &Self::Mode) -> Result<bool, Exception> {
        self.visit("truth")?;
        Ok(!mode.is_empty())
    }
    fn equals(&mut self, mode: &Self::Mode, allowed: &'static str) -> Result<bool, Exception> {
        self.visit(allowed)?;
        Ok(mode.as_ref() == allowed)
    }
    fn repr(&mut self, _: &Self::Mode) -> Result<PyStr, Exception> {
        self.visit("repr")?;
        Ok([cps("unaccepted-"), vec![0xdcff]].concat())
    }
}

#[test]
fn custom_mode_identity_and_lazy_sources_follow_python_evaluation_order() {
    let mut host = Host::new(None);
    let original: Rc<str> = Rc::from("bm25");
    let (width, chosen) = resolve_with(&mut host, Some("int"), original.clone()).unwrap();
    assert_eq!(width, 50);
    assert!(Rc::ptr_eq(&chosen, &original));
    assert_eq!(host.trace, ["int", "truth", "keyword", "bm25"]);
    host.trace.clear();
    let (width, chosen) = resolve_with(&mut host, None, Rc::from("")).unwrap();
    assert_eq!((width, chosen.as_ref()), (39, "hybrid"));
    assert_eq!(
        host.trace,
        [
            "default width",
            "index",
            "truth",
            "default mode",
            "truth",
            "keyword",
            "bm25",
            "hybrid"
        ]
    );
    host.trace.clear();
    let Err(Exception::Own(error)) = resolve_with(&mut host, Some("int"), Rc::from("none")) else {
        panic!("invalid custom mode")
    };
    assert_eq!(
        host.trace,
        ["int", "truth", "keyword", "bm25", "hybrid", "repr"]
    );
    assert_eq!(error.class, "ValueError");
    assert_eq!(
        error.message,
        [
            cps("unknown search mode unaccepted-"),
            vec![0xdcff],
            cps("; must be one of: keyword, bm25, hybrid")
        ]
        .concat()
    );
}

#[test]
fn every_failed_source_returns_its_original_exception_immediately() {
    for (width, mode, stop) in [
        (None, "", "default width"),
        (None, "", "index"),
        (Some("int"), "", "int"),
        (Some("int"), "", "truth"),
        (Some("int"), "", "default mode"),
        (Some("int"), "bad", "bm25"),
        (Some("int"), "bad", "repr"),
    ] {
        let mut host = Host::new(Some(stop));
        let Err(Exception::Source(error)) = resolve_with(&mut host, width, Rc::from(mode)) else {
            panic!("source failure {stop}")
        };
        assert!(Rc::ptr_eq(&error, &host.failure));
        assert_eq!(host.trace.last(), Some(&stop));
    }
}
