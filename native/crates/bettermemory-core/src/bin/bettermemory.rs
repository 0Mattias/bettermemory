//! The native process entry for the bettermemory CLI.

use std::io::{self, Write};

use bettermemory_core::argparse::help::DefaultHelp;
use bettermemory_core::argparse::Outcome;
use bettermemory_core::cli::log_cmd::VerifyArgs;
use bettermemory_core::cli::root::{self, Fault, Formatted, ProductEnded};
use bettermemory_core::cli::Exit;
use bettermemory_core::fsutil::fsdecode;
use bettermemory_core::json::utf8_text;
use bettermemory_core::pyvalue::{int_from_str, PyValue};
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

fn unavailable(feature: &str) -> Exit {
    Exit {
        code: 2,
        stderr: cps(&format!(
            "bettermemory: {feature} is unavailable in this native build\n"
        )),
    }
}

/// `bettermemory log verify`, where this build has the command.
#[cfg(unix)]
fn log_verify(args: VerifyArgs, parser: &Formatted<'_>) -> Outcome {
    bettermemory_core::cli::log_verify::verify(&args, parser, env!("BETTERMEMORY_VERSION"))
}

#[cfg(not(unix))]
fn log_verify(_: VerifyArgs, _: &Formatted<'_>) -> Outcome {
    let Exit { code, stderr } = unavailable("log verify");
    Outcome {
        code,
        stdout: Vec::new(),
        stderr,
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
    let result = root::product_main(
        &argv,
        env!("BETTERMEMORY_VERSION"),
        formatter_width(),
        &DefaultHelp,
        |_| 2,
        || unavailable("the stdio shim"),
        log_verify,
    );
    let exit = match result {
        Ok(ProductEnded::Parser(Outcome {
            code,
            stdout,
            stderr,
        })) => {
            write_text(io::stdout().lock(), &stdout)?;
            Exit { code, stderr }
        }
        // A command's stderr is what it logged while it worked, and the
        // line it ended on when it wrote no report: both are written
        // before the report, as the reference writes them, so the two
        // streams read in that order where they share a terminal or a
        // file, and a report nobody reads does not lose the warnings.
        Ok(ProductEnded::Log(Outcome {
            code,
            stdout,
            stderr,
        })) => {
            write_text(io::stderr().lock(), &stderr)?;
            write_text(io::stdout().lock(), &stdout)?;
            Exit {
                code,
                stderr: Vec::new(),
            }
        }
        Ok(ProductEnded::Shim(exit)) => exit,
        Ok(ProductEnded::Hook(code)) => Exit {
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
