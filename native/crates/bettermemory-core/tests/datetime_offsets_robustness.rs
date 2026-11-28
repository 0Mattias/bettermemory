//! The datetime reader's offsets over inputs no fixture names, under
//! stated difference D1: every offset of hours, minutes and seconds at
//! their bounds with fractions of one to nine digits, both signs, both
//! fraction marks and the compact spellings. A fraction after the seconds
//! is read by D1's rule (the whole seconds and the fraction's first six
//! digits, the sign on both, UTC only when both are zero, refused from a
//! whole day on); one after the hour, the minute or a fourth `:` by
//! 3.13.13's (UTC whenever the whole seconds are zero), and so is every
//! offset after a time whose fraction follows its hour or minute or a
//! fourth `:`. A seeded sweep of the same; fractions of a hundred thousand
//! digits; and every reader over them without a panic, in debug and
//! release. Reading a long fraction is linear: ten times the digits cost
//! under sixty times the time.

mod timing;

use bettermemory_core::datetime::{self, DateError, DateTime};
use bettermemory_core::eval_model as em;

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

const STAMP: &str = "2026-11-15T08:09:10";
const DAY_US: i64 = 86_400_000_000;

/// The offset D1's rule gives `sign hh mm ss` with the fraction `digits`,
/// or None where the offset reaches a whole day.
fn rule(sign: i64, h: i64, m: i64, s: i64, digits: &str) -> Option<i64> {
    let six: String = digits.chars().take(6).collect();
    let micro: i64 = format!("{six:0<6}").parse().expect("digits");
    let us = sign * ((h * 3600 + m * 60 + s) * 1_000_000 + micro);
    (us.abs() < DAY_US).then_some(us)
}

/// The offset 3.13.13's rule gives a fraction that does not follow the
/// seconds (after the hour, the minute or a fourth `:`): UTC when the
/// whole seconds are zero, else as [`rule`].
fn rule_313(sign: i64, h: i64, m: i64, s: i64, digits: &str) -> Option<i64> {
    if h * 3600 + m * 60 + s == 0 {
        Some(0)
    } else {
        rule(sign, h, m, s, digits)
    }
}

fn read(text: &str) -> Result<DateTime, DateError> {
    DateTime::from_isoformat(&cps(text))
}

fn check(text: &str, want: Option<i64>) {
    let got = read(text);
    match want {
        Some(us) => {
            let dt = got.unwrap_or_else(|e| panic!("{text}: {e:?}"));
            assert_eq!(dt.offset_us, Some(us), "{text}");
            assert_eq!(
                (dt.hour, dt.minute, dt.second, dt.microsecond),
                (8, 9, 10, 0),
                "{text}"
            );
        }
        None => assert_eq!(got, Err(DateError::ValueError), "{text}"),
    }
}

fn fractions() -> Vec<String> {
    let mut out = Vec::new();
    for n in 1..=9 {
        out.push("0".repeat(n));
        out.push(format!("1{}", "0".repeat(n - 1)));
        out.push(format!("{}1", "0".repeat(n - 1)));
        out.push("9".repeat(n));
        out.push(format!("{}5", "0".repeat(n - 1)));
    }
    out
}

#[test]
fn every_bound_of_an_offset_reads_by_the_rule() {
    let mut checked = 0;
    for (sign, mark) in [(1, '+'), (-1, '-')] {
        for h in [0, 1, 12, 23, 24, 99] {
            for m in [0, 1, 59, 60, 99] {
                for s in [0, 1, 59, 60, 99] {
                    for frac in fractions() {
                        for point in ['.', ','] {
                            let want = rule(sign, h, m, s, &frac);
                            check(
                                &format!("{STAMP}{mark}{h:02}:{m:02}:{s:02}{point}{frac}"),
                                want,
                            );
                            check(
                                &format!("{STAMP}{mark}{h:02}{m:02}{s:02}{point}{frac}"),
                                want,
                            );
                            checked += 2;
                        }
                        // Digits straight after the compact seconds: the
                        // fraction when there are two or more, a refusal
                        // for one (the C parser's end check).
                        let want = (frac.len() > 1).then(|| rule(sign, h, m, s, &frac));
                        check(
                            &format!("{STAMP}{mark}{h:02}{m:02}{s:02}{frac}"),
                            want.flatten(),
                        );
                        // After a fourth `:`: 3.13.13's rule.
                        check(
                            &format!("{STAMP}{mark}{h:02}:{m:02}:{s:02}:{frac}"),
                            rule_313(sign, h, m, s, &frac),
                        );
                        checked += 2;
                    }
                }
                // A fraction after the minutes or after the hours:
                // 3.13.13's rule.
                for frac in ["5", "000001", "0000001"] {
                    for point in ['.', ','] {
                        check(
                            &format!("{STAMP}{mark}{h:02}:{m:02}{point}{frac}"),
                            rule_313(sign, h, m, 0, frac),
                        );
                        check(
                            &format!("{STAMP}{mark}{h:02}{m:02}{point}{frac}"),
                            rule_313(sign, h, m, 0, frac),
                        );
                        check(
                            &format!("{STAMP}{mark}{h:02}{point}{frac}"),
                            rule_313(sign, h, 0, 0, frac),
                        );
                        checked += 3;
                    }
                }
            }
        }
    }
    assert!(checked > 30_000, "{checked} texts");
}

/// A time's hour, minute, second and microsecond.
type Wall = (u32, u32, u32, u32);

#[test]
fn every_time_spelling_before_an_offset_reads_by_the_rule() {
    // (time, its wall clock, whether 3.14 reads it): a time 3.14 refuses
    // (a fraction after its hour or minute or a fourth `:`) reads its
    // offset by 3.13.13's rule, every other by D1's.
    let times: [(&str, Wall, bool); 15] = [
        ("08:09:10", (8, 9, 10, 0), true),
        ("08:09:10.5", (8, 9, 10, 500_000), true),
        ("08:09:10,123456789", (8, 9, 10, 123_456), true),
        ("080910", (8, 9, 10, 0), true),
        ("080910123", (8, 9, 10, 123_000), true),
        ("08:09", (8, 9, 0, 0), true),
        ("0809", (8, 9, 0, 0), true),
        ("08", (8, 0, 0, 0), true),
        ("08,5", (8, 0, 0, 500_000), false),
        ("08.5", (8, 0, 0, 500_000), false),
        ("08:09.5", (8, 9, 0, 500_000), false),
        ("08:09,25", (8, 9, 0, 250_000), false),
        ("0809.5", (8, 9, 0, 500_000), false),
        ("08:09:10:5", (8, 9, 10, 500_000), false),
        ("08:09:10:000001", (8, 9, 10, 1), false),
    ];
    let mut checked = 0;
    for (time, wall, reads) in times {
        for (sign, mark) in [(1, '+'), (-1, '-')] {
            for (h, m, s) in [(0, 0, 0), (0, 0, 1), (0, 1, 0), (1, 0, 0), (23, 59, 59)] {
                for frac in fractions() {
                    let want = if reads {
                        rule(sign, h, m, s, &frac)
                    } else {
                        rule_313(sign, h, m, s, &frac)
                    };
                    let mut texts = vec![
                        format!("2026-11-15T{time}{mark}{h:02}:{m:02}:{s:02}.{frac}"),
                        format!("2026-11-15T{time}{mark}{h:02}{m:02}{s:02},{frac}"),
                    ];
                    if frac.len() > 1 {
                        texts.push(format!("2026-11-15T{time}{mark}{h:02}{m:02}{s:02}{frac}"));
                    }
                    for text in texts {
                        let dt = read(&text).unwrap_or_else(|e| panic!("{text}: {e:?}"));
                        assert_eq!(dt.offset_us, want, "{text}");
                        assert_eq!(
                            (dt.hour, dt.minute, dt.second, dt.microsecond),
                            wall,
                            "{text}"
                        );
                        checked += 1;
                    }
                }
            }
        }
    }
    assert!(checked > 15_000, "{checked} texts");
}

/// The generator's draw: x = x * 6364136223846793005 + 1442695040888963407
/// mod 2**64, the high 31 bits.
struct Draw(u64);

impl Draw {
    fn pick(&mut self, n: u64) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (self.0 >> 33) % n
    }
}

#[test]
fn a_seeded_sweep_reads_by_the_rule() {
    let mut d = Draw(20_261_127);
    for _ in 0..50_000 {
        let sign = if d.pick(2) == 0 { 1 } else { -1 };
        // Mostly zero whole parts, the case D1 is about.
        let mut part = |hi: u64| if d.pick(3) == 0 { d.pick(hi) as i64 } else { 0 };
        let (h, m, s) = (part(24), part(60), part(60));
        let n = 1 + d.pick(12) as usize;
        let frac: String = (0..n)
            .map(|_| char::from(b'0' + if d.pick(4) == 0 { d.pick(10) as u8 } else { 0 }))
            .collect();
        let mark = if sign > 0 { '+' } else { '-' };
        // After the seconds, extended or compact, with either mark: D1's
        // rule; after a fourth `:` or the minutes: 3.13.13's.
        let point = if d.pick(2) == 0 { '.' } else { ',' };
        let (text, want) = match d.pick(4) {
            0 => (
                format!("{STAMP}{mark}{h:02}:{m:02}:{s:02}{point}{frac}"),
                rule(sign, h, m, s, &frac),
            ),
            1 => (
                format!("{STAMP}{mark}{h:02}{m:02}{s:02}{point}{frac}"),
                rule(sign, h, m, s, &frac),
            ),
            2 => (
                format!("{STAMP}{mark}{h:02}:{m:02}:{s:02}:{frac}"),
                rule_313(sign, h, m, s, &frac),
            ),
            _ => (
                format!("{STAMP}{mark}{h:02}:{m:02}{point}{frac}"),
                rule_313(sign, h, m, 0, &frac),
            ),
        };
        check(&text, want);
    }
}

#[test]
fn every_reader_takes_hostile_offsets() {
    let now = DateTime::new(2026, 11, 25, 12, 0, 0, 0, Some(0)).expect("now");
    let long = "1".repeat(100_000);
    let texts = [
        format!("{STAMP}+00:00:00.{long}"),
        format!("{STAMP}-00:00:00.{}", "0".repeat(100_000)),
        format!("{STAMP}-00:00:00.{}1", "0".repeat(99_999)),
        format!("{STAMP}+00:00:00."),
        format!("{STAMP}+00:00:00.x"),
        format!("{STAMP}+00:00:00.5Z"),
        format!("{STAMP}+00:00:00.5\u{0}"),
        format!("{STAMP}+00:00:00.\u{0665}"),
        format!("{STAMP}+23:59:59.999999"),
        format!("{STAMP}-23:59:59.9999999"),
        format!("{STAMP}+23:59:60"),
        format!("{STAMP}-24:00:00.000001"),
        format!("{STAMP}.999999-00:00:00.000001"),
        "0001-01-01T00:00:00+00:00:00.000001".to_string(),
        "9999-12-31T23:59:59.999999-00:00:00.000001".to_string(),
        format!("{STAMP}+00:00:00.5Z").replace('T', "Z"),
    ];
    for text in &texts {
        let t = cps(text);
        let _ = DateTime::from_isoformat(&t);
        let _ = datetime::parse_event_ts(&t);
        let _ = datetime::parse_record_dt(&t);
        let _ = datetime::git_instant(&t);
        for flag in ["--since", "--until"] {
            let _ = em::parse_timestamp_utc(&t, flag);
            let _ = em::parse_window_bound(Some(t.as_slice()), flag, &now);
        }
    }
    // The first six digits, the rest skipped.
    check(&texts[0], Some(111_111));
    check(&texts[1], Some(0));
    check(&texts[2], Some(0));
    check(&texts[8], Some(DAY_US - 1));
    check(&texts[9], Some(-(DAY_US - 1)));
    check(&texts[10], None);
    check(&texts[11], None);
    // The calendar's ends: read, then refused once taken to UTC.
    for flag in ["--since", "--until"] {
        assert!(em::parse_timestamp_utc(&cps(&texts[13]), flag).is_err());
        assert!(em::parse_timestamp_utc(&cps(&texts[14]), flag).is_err());
    }
    assert!(datetime::parse_event_ts(&cps(&texts[13])).is_some());
}

#[test]
fn a_long_fraction_reads_in_linear_time() {
    let text = |n: usize| cps(&format!("{STAMP}-00:00:00.{}", "3".repeat(n)));
    let (small, large) = (text(100_000), text(1_000_000));
    timing::grows_within(
        "datetime_offsets_long_fraction",
        timing::TENFOLD,
        &mut || timing::once(|| DateTime::from_isoformat(&small).unwrap()),
        &mut || timing::once(|| DateTime::from_isoformat(&large).unwrap()),
    );
}
