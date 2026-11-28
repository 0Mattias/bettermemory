//! Stated difference D1 against plausible wrong readers: one that still
//! collapses an offset of zero whole seconds with a fraction after the
//! seconds to UTC (as CPython 3.13.13 does), one that keeps every fraction
//! (a seventh digit, a fraction of zeros), one that also keeps a fraction
//! after the offset's hour or minute or a fourth `:` (which 3.14 refuses
//! and 3.13.13 reads as UTC), one that keeps it after a time whose
//! fraction follows its hour or minute or a fourth `:` (the same), one
//! that applies the sign twice, and one that keeps the offset in
//! `from_isoformat` but loses it in a caller.
//! And `DateTime::new` against one that takes an offset of a whole day.

use bettermemory_core::cli::common::parse_iso_cutoff;
use bettermemory_core::datetime::{self, DateError, DateTime};
use bettermemory_core::eval_model::{self as em, Bound};
use bettermemory_core::pyvalue::PyValue;

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn utc(y: i64, mo: u32, d: u32, h: u32, mi: u32, s: u32, us: u32) -> DateTime {
    DateTime::new(y, mo, d, h, mi, s, us, Some(0)).expect("a datetime")
}

const STAMP: &str = "2026-11-15T08:09:10";

/// The three texts that left `datetime_edges.jsonl`, each with the offset
/// it keeps (microseconds) and the instant it names in UTC.
fn d1_texts() -> [(String, i64, DateTime); 3] {
    [
        (
            format!("{STAMP}+00:00:00.5"),
            500_000,
            utc(2026, 11, 15, 8, 9, 9, 500_000),
        ),
        (
            format!("{STAMP}+00:00:00.000001"),
            1,
            utc(2026, 11, 15, 8, 9, 9, 999_999),
        ),
        (
            format!("{STAMP}-00:00:00.000001"),
            -1,
            utc(2026, 11, 15, 8, 9, 10, 1),
        ),
    ]
}

fn wall(dt: &DateTime) -> (i64, u32, u32, u32, u32, u32, u32) {
    (
        i64::from(dt.year),
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        dt.microsecond,
    )
}

#[test]
fn d1_texts_keep_their_offsets_through_every_reader() {
    for (text, offset, _) in d1_texts() {
        let t = cps(&text);
        let read = [
            ("from_isoformat", DateTime::from_isoformat(&t).ok()),
            ("parse_event_ts", datetime::parse_event_ts(&t)),
            ("parse_record_dt", datetime::parse_record_dt(&t).ok()),
            ("git_instant", datetime::git_instant(&t)),
            (
                "git_instant, padded",
                datetime::git_instant(&cps(&format!(" {text}\n"))),
            ),
        ];
        for (reader, got) in read {
            let got = got.unwrap_or_else(|| panic!("{reader} reads {text}"));
            assert_eq!(got.offset_us, Some(offset), "{reader}({text})");
            assert_eq!(wall(&got), (2026, 11, 15, 8, 9, 10, 0), "{reader}({text})");
        }
    }
}

#[test]
fn d1_texts_are_not_read_as_utc() {
    // The reference under 3.13.13: the same wall clock at UTC.
    let collapsed = "2026-11-15T08:09:10+00:00";
    for (text, _, _) in d1_texts() {
        let got = datetime::parse_event_ts(&cps(&text)).expect("read");
        assert_ne!(got.isoformat(), collapsed, "{text}");
        assert_ne!(got.offset_us, Some(0), "{text}");
    }
    let spelled: Vec<String> = d1_texts()
        .iter()
        .map(|(t, _, _)| datetime::parse_record_dt(&cps(t)).unwrap().isoformat())
        .collect();
    assert_eq!(
        spelled,
        [
            "2026-11-15T08:09:10+00:00:00.500000",
            "2026-11-15T08:09:10+00:00:00.000001",
            "2026-11-15T08:09:10-00:00:00.000001",
        ]
    );
}

#[test]
fn eval_model_bounds_read_the_kept_offset() {
    let now = utc(2026, 11, 25, 12, 0, 0, 0);
    for (text, _, instant) in d1_texts() {
        let t = cps(&text);
        let at = em::parse_timestamp_utc(&t, "--until").expect("a bound");
        assert_eq!(at, instant, "parse_timestamp_utc({text})");
        match em::parse_window_bound(Some(t.as_slice()), "--until", &now) {
            Ok(Some(Bound::At(at))) => assert_eq!(at, instant, "parse_window_bound({text})"),
            other => panic!("parse_window_bound({text}): {other:?}"),
        }
    }
    // A microsecond after now: refused as later than now, where the
    // collapsed reading is now itself and accepted.
    let late = cps("2026-11-25T12:00:00-00:00:00.000001");
    assert!(em::parse_window_bound(Some(late.as_slice()), "--since", &now).is_err());
    // A microsecond before the calendar: refused, where the collapsed
    // reading is its first instant.
    let early = cps("0001-01-01T00:00:00+00:00:00.000001");
    assert!(em::parse_window_bound(Some(early.as_slice()), "--since", &now).is_err());
}

#[test]
fn the_window_test_reads_the_kept_offset() {
    let show = |ts: &str| PyValue::Dict(vec![(PyValue::str("ts"), PyValue::str(ts))]);
    let start = utc(2026, 1, 1, 0, 0, 0, 0);
    let end = utc(2026, 2, 1, 0, 0, 0, 0);
    // A microsecond before the start, and at the end itself.
    let before = show("2026-01-01T00:00:00+00:00:00.000001");
    assert_eq!(em::show_in_window(&before, Some(&start), None), Ok(false));
    let at_end = show("2026-01-31T23:59:59.999999-00:00:00.000001");
    assert_eq!(em::show_in_window(&at_end, None, Some(&end)), Ok(false));
    // Half a second late on the wall clock, at the start in UTC.
    let at_start = show("2025-12-31T23:59:59.5-00:00:00.5");
    assert_eq!(em::show_in_window(&at_start, Some(&start), None), Ok(true));
}

#[test]
fn the_cli_cutoff_reads_the_kept_offset() {
    let now = utc(2026, 11, 25, 12, 0, 0, 0);
    let flag = cps("rollback --since");
    for (text, offset, _) in d1_texts() {
        let got = parse_iso_cutoff(&cps(&text), &flag, "warn", &now)
            .unwrap_or_else(|_| panic!("{text} is a cutoff"));
        assert_eq!(got.parsed.offset_us, Some(offset), "{text}");
    }
    let got = parse_iso_cutoff(&cps(&format!("{STAMP}+00:00:00.5")), &flag, "warn", &now)
        .unwrap_or_else(|_| panic!("a cutoff"));
    assert_eq!(got.canonical, cps("2026-11-15T08:09:09.500000Z"));
}

#[test]
fn zero_fractions_stay_utc() {
    // Six digits of zeros, one, a seventh digit the parser drops, and a
    // negative zero: UTC, as every release reads them.
    for tail in [
        "+00:00:00.000000",
        "+00:00:00.0",
        "+00:00:00.0000001",
        "-00:00:00.0000009",
        "-00:00:00.000000",
        "+00:00:00,0",
        "-00:00",
        "+00:00:00",
    ] {
        let text = format!("{STAMP}{tail}");
        let got = DateTime::from_isoformat(&cps(&text)).expect("read");
        assert_eq!(got.offset_us, Some(0), "{text}");
        assert_eq!(got.isoformat(), "2026-11-15T08:09:10+00:00", "{text}");
    }
}

#[test]
fn the_sign_applies_once() {
    let offset = |tail: &str| {
        DateTime::from_isoformat(&cps(&format!("{STAMP}{tail}")))
            .expect("read")
            .offset_us
    };
    assert_eq!(offset("-00:00:00.000001"), Some(-1));
    assert_eq!(offset("-00:00:00.5"), Some(-500_000));
    assert_eq!(offset("+00:00:00.5"), Some(500_000));
    // The other spellings of a fraction after the seconds, which 3.14.10
    // and 3.15.2 keep too: a comma, the compact seconds with either mark,
    // and digits straight after the compact seconds.
    assert_eq!(offset("-00:00:00,25"), Some(-250_000));
    assert_eq!(offset("-000000.5"), Some(-500_000));
    assert_eq!(offset("+000000,5"), Some(500_000));
    assert_eq!(offset("-00000085"), Some(-850_000));
    // Whole seconds with a fraction: unchanged by D1.
    assert_eq!(offset("-01:00:00.5"), Some(-3_600_500_000));
    assert_eq!(offset("+00:00:01.5"), Some(1_500_000));
}

#[test]
fn a_fraction_before_the_seconds_reads_as_3_13_13() {
    // A fraction after the offset's hour or minute, or after a fourth
    // `:`: 3.14.10 and 3.15.2 refuse the text, and 3.13.13 reads UTC with
    // the fraction dropped, as this build does (D1 is a fraction after
    // the seconds only).
    for tail in [
        "-00.5",
        "+00,5",
        "+00:00.5",
        "-00:00,25",
        "+0000.5",
        "-0000,5",
        "+00:00:00:5",
        "-00:00:00:000001",
    ] {
        let text = format!("{STAMP}{tail}");
        let got = DateTime::from_isoformat(&cps(&text)).expect("read");
        assert_eq!(got.offset_us, Some(0), "{text}");
        assert_eq!(got.isoformat(), "2026-11-15T08:09:10+00:00", "{text}");
        let event = datetime::parse_event_ts(&cps(&text)).expect("read");
        assert_eq!(event.offset_us, Some(0), "parse_event_ts({text})");
    }
    // With whole seconds the fraction is kept, as 3.13.13 keeps it.
    let offset = |tail: &str| {
        DateTime::from_isoformat(&cps(&format!("{STAMP}{tail}")))
            .expect("read")
            .offset_us
    };
    assert_eq!(offset("+02.5"), Some(7_200_500_000));
    assert_eq!(offset("-00:01,5"), Some(-60_500_000));
    assert_eq!(offset("+0001.5"), Some(60_500_000));
    assert_eq!(offset("+00:00:01:5"), Some(1_500_000));
}

#[test]
fn a_time_3_14_refuses_reads_as_3_13_13() {
    // A time with a fraction after its hour or minute, or after a fourth
    // `:`, or ending on a bare `.` or `,`, before an offset D1 keeps
    // elsewhere: 3.14.10 and 3.15.2 refuse
    // the whole text, and 3.13.13 reads the time with UTC, as this build
    // does (D1 holds only in a text 3.14 reads).
    for (text, iso) in [
        (
            "2026-11-15T08,5+00:00:00.5",
            "2026-11-15T08:00:00.500000+00:00",
        ),
        (
            "2026-11-15T08.5-00:00:00.000001",
            "2026-11-15T08:00:00.500000+00:00",
        ),
        (
            "2026-11-15T08:09.5-000000.5",
            "2026-11-15T08:09:00.500000+00:00",
        ),
        (
            "2026-11-15T0809.5+000000,25",
            "2026-11-15T08:09:00.500000+00:00",
        ),
        (
            "2026-11-15T08:09,5-00000085",
            "2026-11-15T08:09:00.500000+00:00",
        ),
        (
            "2026-11-15T08:09:10:5+00:00:00.5",
            "2026-11-15T08:09:10.500000+00:00",
        ),
        (
            "2026-11-15 08:09:10:5-00:00:00.000001",
            "2026-11-15T08:09:10.500000+00:00",
        ),
        // A bare decimal mark ends the time: 3.13.13 reads no fraction.
        (
            "2026-11-15T08:09:10.+00:00:00.5",
            "2026-11-15T08:09:10+00:00",
        ),
        (
            "2026-11-15T08,-00:00:00.000001",
            "2026-11-15T08:00:00+00:00",
        ),
        ("2026-11-15T0809,+000000,25", "2026-11-15T08:09:00+00:00"),
        ("2026-11-15 08:09:10,-00000085", "2026-11-15T08:09:10+00:00"),
    ] {
        let t = cps(text);
        let read = [
            ("from_isoformat", DateTime::from_isoformat(&t).ok()),
            ("parse_event_ts", datetime::parse_event_ts(&t)),
            ("parse_record_dt", datetime::parse_record_dt(&t).ok()),
            ("git_instant", datetime::git_instant(&t)),
            (
                "parse_timestamp_utc",
                em::parse_timestamp_utc(&t, "--since").ok(),
            ),
        ];
        for (reader, got) in read {
            let got = got.unwrap_or_else(|| panic!("{reader} reads {text}"));
            assert_eq!(got.offset_us, Some(0), "{reader}({text})");
            assert_eq!(got.isoformat(), iso, "{reader}({text})");
        }
    }
    // With whole seconds in the offset, 3.13.13 keeps its fraction there.
    let got = DateTime::from_isoformat(&cps("2026-11-15T08,5+00:00:01.5")).expect("read");
    assert_eq!(got.offset_us, Some(1_500_000));
    // A time 3.14 reads keeps the offset: no fraction, the hour alone,
    // the compact minutes, digits straight after the compact seconds, a
    // fraction after the seconds in either mark.
    for (text, offset) in [
        ("2026-11-15T08:09+00:00:00.5", 500_000),
        ("2026-11-15T08-00:00:00.000001", -1),
        ("2026-11-15T0809-00:00:00.5", -500_000),
        ("2026-11-15T080910123-00:00:00.5", -500_000),
        ("2026-11-15T08:09:10.5-00:00:00.5", -500_000),
        ("2026-11-15T080910,5+000000,25", 250_000),
        ("20261115T08:09:10,25-000000.5", -500_000),
    ] {
        let got = DateTime::from_isoformat(&cps(text)).expect("read");
        assert_eq!(got.offset_us, Some(offset), "{text}");
    }
}

#[test]
fn an_offset_of_a_whole_day_is_refused() {
    // `timezone(timedelta(days=1))` and `timezone(timedelta(days=-1))`
    // raise ValueError; a microsecond less is an offset.
    const DAY_US: i64 = 86_400_000_000;
    for us in [DAY_US, -DAY_US] {
        assert_eq!(
            DateTime::new(2026, 11, 15, 8, 9, 10, 0, Some(us)),
            Err(DateError::ValueError),
            "offset {us}"
        );
    }
    for us in [DAY_US - 1, 1 - DAY_US] {
        let dt = DateTime::new(2026, 11, 15, 8, 9, 10, 0, Some(us)).expect("an offset");
        assert_eq!(dt.offset_us, Some(us));
    }
}

#[test]
fn what_a_rewrite_of_a_kept_offset_stores() {
    // A writer that reads a stored record and writes it back re-dumps its
    // datetimes: the column through `isoformat`, the signed payload
    // through pydantic's JSON spelling (native/README.md, "The datetime
    // reader"). CPython 3.13.13 writes `+00:00` and `Z` for every one.
    let cases = [
        (
            "2026-03-01T12:00:00+00:00:00.5",
            "2026-03-01T12:00:00+00:00:00.500000",
            "2026-03-01T12:00:00+00:00",
        ),
        (
            "2026-03-01T12:00:00-00:00:00.5",
            "2026-03-01T12:00:00-00:00:00.500000",
            "2026-03-01T12:00:00-00:00",
        ),
        (
            "2026-03-01T12:00:00+00:00:00.000001",
            "2026-03-01T12:00:00+00:00:00.000001",
            "2026-03-01T12:00:00Z",
        ),
        (
            "2026-03-01T12:00:00-00:00:00.000001",
            "2026-03-01T12:00:00-00:00:00.000001",
            "2026-03-01T12:00:00Z",
        ),
    ];
    for (stored, column, payload) in cases {
        let read = datetime::parse_record_dt(&cps(stored)).expect("read");
        assert_eq!(read.isoformat(), column, "{stored}");
        assert_eq!(read.pydantic_json(), payload, "{stored}");
    }
}
