//! Format the whole report before a caller writes any bytes.
use bettermemory_core::cli::log_report::{format_output, ReportError};
use bettermemory_core::{json, unicode::cps};
#[test]
fn printable_runs_after_fingerprint_slicing_and_preserves_a_final_lf() {
    let doc=json::loads(&cps(r#"{"status":"ok","path":"a\udcff","store_id":"\u001b","rows":9,"segments":[{"first_seq":1,"last_seq":9,"status":"ok","key_fingerprint":"0123456789abcde\u001bX"}],"head":{"status":"ok","seq":0},"fold":{"status":"ok","tables":{}},"problems":[],"anchors":[]}"#)).unwrap();
    let (code, text) = format_output(&doc, false, false).unwrap();
    assert_eq!(code, 0);
    assert!(text.contains("  store a\\udcff\n  id \\x1b\n"));
    assert!(text.contains("head ok at seq 0\n"));
    assert!(text.contains("key 0123456789abcde\\x1b)\n"));
    assert!(text.ends_with("  anchors 0\n"));
    assert_eq!(
        format_output(&doc, true, true),
        Err(ReportError::BytesLabel)
    );
}

#[test]
fn membership_total_checks_the_printed_integer_after_cancellation() {
    let make_report = |counts: &[&str]| {
        let tables = counts
            .iter()
            .enumerate()
            .map(|(index, rows)| {
                format!(r#""t{index}":{{"rows":"{rows}","unaccounted":[],"missing":[]}}"#)
            })
            .collect::<Vec<_>>()
            .join(",");
        let doc = format!(
            r#"{{"status":"ok","path":"p","store_id":"s","rows":0,"segments":[],"head":{{"status":"ok"}},"fold":{{"status":"ok","tables":{{}},"membership":{{{tables}}}}},"problems":[]}}"#
        );
        json::loads(&cps(&doc)).unwrap()
    };
    let nines = "9".repeat(4300);
    let neg = format!("-{nines}");
    let cancelled = make_report(&[&neg, "-1", &nines, "1"]);
    assert!(format_output(&cancelled, false, false)
        .unwrap()
        .1
        .contains("  membership 0 rows in 4 tables\n"));
    let overflowing = make_report(&[&nines, "1"]);
    let error = format_output(&overflowing, false, false).unwrap_err();
    assert_eq!(error, ReportError::IntegerDisplayLimit);
    assert_eq!(error.to_string(), "Exceeds the limit (4300 digits) for integer string conversion; use sys.set_int_max_str_digits() to increase the limit");
    assert!(format_output(&overflowing, false, true).is_ok());
}
