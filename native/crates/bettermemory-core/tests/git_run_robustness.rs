//! Hostile inputs to `git_run`: very large streams, many arguments, a
//! lone surrogate, float extremes; no panic, linear time.

mod timing;

use std::sync::atomic::AtomicI64;

use bettermemory_core::git_calls::Tally;
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::git_run::{self as gr, Call, GitProcess, Out, Spawned};
use bettermemory_core::gitsystem::ask::Env;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

struct Fixed(Spawned);

impl GitProcess for Fixed {
    fn run(
        &mut self,
        _argv: &[PyStr],
        _cwd: Option<&[u32]>,
        _env: &Env,
        _stdin: Option<&[u8]>,
        _timeout: f64,
    ) -> Spawned {
        self.0.clone()
    }
}

fn call_with(p: &mut Fixed, call: &Call<'_>, env: &Env) -> gr::Ran {
    gr::run_git(
        &Scope::default(),
        &|| 0.0,
        &AtomicI64::new(0),
        &mut Tally::default(),
        p,
        env,
        call,
        || false,
    )
}

#[test]
fn invalid_utf8_streams_decode_in_linear_time() {
    let make = |n: usize| -> Vec<u8> {
        (0..n)
            .map(|i| [0xff, b'\r', 0xe2, 0x82, b'a'][i % 5])
            .collect()
    };
    let run = |bytes: Vec<u8>| {
        let n = bytes.len();
        let args = vec![cps("log")];
        let mut p = Fixed(Spawned::Exited {
            returncode: 0,
            stdout: bytes.clone(),
            stderr: bytes,
        });
        let (got, _) = call_with(&mut p, &Call::new(&args), &Vec::new());
        match got.unwrap().unwrap().stdout {
            Out::Text(t) => assert!(t.len() <= n),
            Out::Bytes(_) => panic!("text"),
        }
    };
    timing::grows_within(
        "decode: 200,000 and 2,000,000 bytes",
        timing::TENFOLD,
        &mut || {
            let b = make(200_000);
            timing::once(|| run(b))
        },
        &mut || {
            let b = make(2_000_000);
            timing::once(|| run(b))
        },
    );
}

#[test]
fn many_arguments_and_variables() {
    let mut args: Vec<PyStr> = Vec::new();
    for _ in 0..50_000 {
        args.push(cps("-c"));
        args.push(cps("a=b"));
    }
    let env: Env = (0..50_000)
        .map(|i| (cps(&format!("V{i}")), cps("x")))
        .collect();
    let (got, lines) = call_with(&mut Fixed(Spawned::TimedOut), &Call::new(&args), &env);
    assert_eq!(got, Ok(None));
    assert_eq!(lines[0].1, cps("git  timed out after 1.0s in None"));
}

#[test]
fn lone_surrogates_and_float_extremes() {
    let args = vec![vec![0xd800], cps("log")];
    let cwd = vec![0x2f, 0xdc80];
    for t in [f64::MAX, f64::MIN_POSITIVE, 5e-324, f64::INFINITY, -0.0] {
        let call = Call {
            cwd: Some(&cwd),
            timeout: t,
            ..Call::new(&args)
        };
        let (_, lines) = call_with(
            &mut Fixed(Spawned::NotFound(vec![0xdfff])),
            &call,
            &Vec::new(),
        );
        assert_eq!(
            lines[0].1,
            [
                cps("git cannot run in "),
                cwd.clone(),
                cps(": the directory does not exist")
            ]
            .concat()
        );
        let (_, lines) = call_with(&mut Fixed(Spawned::TimedOut), &call, &Vec::new());
        assert_eq!(lines[0].1[..5], [cps("git "), vec![0xd800]].concat()[..]);
    }
    let (_, m) = gr::log_nonzero_exit(&args, i64::MIN, &Out::Bytes(vec![0x80; 100_000]), None);
    assert!(m.len() > 100_000);
}
