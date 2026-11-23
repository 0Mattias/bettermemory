//! The native process entry for the bettermemory CLI.

use std::io::{self, Write};

use bettermemory_core::argparse::{HelpText, Outcome};
use bettermemory_core::cli::root::{self, Ended, Fault};
use bettermemory_core::cli::Exit;
use bettermemory_core::fsutil::fsdecode;
use bettermemory_core::json::utf8_text;
use bettermemory_core::pyvalue::{int_from_str, PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn formatter_width() -> i64 {
    // shutil.get_terminal_size accepts Python integers, including Unicode
    // decimal digits and internal underscores. Ignore a non-positive value.
    let explicit = std::env::var_os("COLUMNS").and_then(|value| {
        let PyValue::Int(ref decimal) = int_from_str(&fsdecode(&value)).ok()? else {
            return None;
        };
        if decimal == "0" || decimal.starts_with('-') {
            return None;
        }
        Some(decimal.parse::<i64>().unwrap_or(i64::MAX))
    });
    let columns = explicit
        .or_else(|| bettermemory_sys::stdout_columns().map(i64::from))
        .unwrap_or(root::DEFAULT_WIDTH + 2);
    columns - 2
}

struct VersionText;

impl HelpText for VersionText {
    // The canonical product version and program name are single words.
    // textwrap moves a whole word to the next line if it fits there. For
    // a word longer than a full line it uses any space left on this line.
    fn fill(&self, text: &[u32], width: i64) -> Option<PyStr> {
        let width = usize::try_from(width).ok().filter(|&w| w > 0)?;
        let mut out = Vec::new();
        let mut used = 0;
        for mut word in text.split(|&cp| cp == u32::from(b' ')) {
            if used > 0 {
                if used + 1 >= width || (word.len() <= width && used + 1 + word.len() > width) {
                    out.push(u32::from(b'\n'));
                    used = 0;
                } else {
                    out.push(u32::from(b' '));
                    used += 1;
                }
            }
            while word.len() > width - used {
                let (part, rest) = word.split_at(width - used);
                out.extend_from_slice(part);
                out.push(u32::from(b'\n'));
                word = rest;
                used = 0;
            }
            out.extend_from_slice(word);
            used += word.len();
        }
        Some(out)
    }
}

fn unavailable(feature: &str) -> Exit {
    Exit {
        code: 2,
        stderr: cps(&format!(
            "bettermemory: {feature} is unavailable in this native build\n"
        )),
    }
}

fn write_text(mut stream: impl Write, text: &[u32]) -> io::Result<()> {
    if !text.is_empty() {
        let encoded = utf8_text(text);
        #[cfg(windows)]
        let encoded = encoded.replace('\n', "\r\n");
        stream.write_all(encoded.as_bytes())?;
        stream.flush()?;
    }
    Ok(())
}

fn run() -> io::Result<i32> {
    let argv: Vec<_> = std::env::args_os()
        .skip(1)
        .map(|arg| fsdecode(&arg))
        .collect();
    let result = root::main(
        &argv,
        env!("BETTERMEMORY_VERSION"),
        &[],
        formatter_width(),
        &VersionText,
        |_| 2,
        || unavailable("the stdio shim"),
    );
    let exit = match result {
        Ok(Ended::Parser(Outcome {
            code,
            stdout,
            stderr,
        })) => {
            write_text(io::stdout().lock(), &stdout)?;
            Exit { code, stderr }
        }
        Ok(Ended::Shim(exit) | Ended::Ran { exit, .. }) => exit,
        Ok(Ended::Hook(code)) => Exit {
            code,
            ..unavailable("the hook client")
        },
        Err(Fault::NotPorted) => unavailable("the requested command or help text"),
        Err(Fault::Spec(error)) => Exit {
            code: 1,
            stderr: cps(&format!(
                "bettermemory: invalid native command specification: {error:?}\n"
            )),
        },
    };
    write_text(io::stderr().lock(), &exit.stderr)?;
    Ok(exit.code)
}

fn main() {
    let code = run().unwrap_or_else(|error| {
        let _ = writeln!(io::stderr().lock(), "bettermemory: {error}");
        120
    });
    std::process::exit(code);
}
