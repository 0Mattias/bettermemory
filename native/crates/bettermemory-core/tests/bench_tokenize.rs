//! A timing, not a check: `tokenize` over every input of `lexicon.jsonl`,
//! repeated, text in and tokens out, as the reference is timed: a `&str`
//! through `tokenize_str`, the inputs holding a lone surrogate (no `&str`
//! can) as code points through `tokenize`. Run it in release mode:
//!
//! ```text
//! cargo test --offline --release --test bench_tokenize -- --ignored --nocapture
//! ```
//!
//! `BM_BENCH_REPS` sets the repetitions (default 50);
//! `native/tools/bench_tokenize.py` times the reference the same way.

mod common;

use std::time::Instant;

use bettermemory_core::lexicon;
use common::{jsonl, text_field};

#[test]
#[ignore = "a timing; run with --ignored in release mode"]
fn bench_tokenize_lexicon_inputs() {
    let reps: usize = std::env::var("BM_BENCH_REPS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(50);
    let lines = jsonl("lexicon.jsonl");
    let cps: Vec<Vec<u32>> = lines[1..].iter().map(|r| text_field(r, "input")).collect();
    let chars: usize = cps.iter().map(Vec::len).sum();
    let inputs: Vec<Result<String, Vec<u32>>> = cps
        .into_iter()
        .map(|c| {
            c.iter()
                .map(|&x| char::from_u32(x))
                .collect::<Option<String>>()
                .ok_or(c)
        })
        .collect();
    let mut tokens = 0;
    let start = Instant::now();
    for _ in 0..reps {
        for input in &inputs {
            tokens += match input {
                Ok(text) => lexicon::tokenize_str(text).len(),
                Err(cps) => lexicon::tokenize(cps).len(),
            };
        }
    }
    let elapsed = start.elapsed();
    println!(
        "rust tokenize: {} inputs ({chars} code points) x {reps} reps = {:.3} s, {:.3} ms per rep, {tokens} tokens",
        inputs.len(),
        elapsed.as_secs_f64(),
        elapsed.as_secs_f64() * 1000.0 / reps as f64
    );
}
