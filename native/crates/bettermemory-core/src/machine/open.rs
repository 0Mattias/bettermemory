//! `machine.py`, unit 3: `_write_record` and `open_machine`, and the
//! machine's own hardware reads (`StdMachine`).
//!
//! Every effect but the two writes is an argument: the files are read
//! through [`Fs`], the id file's object through [`IdFileStat`], the two
//! facts through closures, the clock as one value (at most one record is
//! written per open) and the random bytes through a closure asked only on
//! a mint. The writes go through the store's atomic writer
//! (`replace_retrying`) and the directory through `owner_only_dir`, as
//! `gitsystem::probe` uses them; each written file is then set to 0600,
//! the mode `atomic_write_bytes(..., mode_before_rename=0o600)` leaves.
//!
//! Under the Windows flavour `open_machine` declines (`NotPorted`) before
//! touching anything: the crate's stated Windows gap (ENG-124).

use std::io;
use std::path::Path;
use std::sync::Mutex;

use super::facts::{
    current_hostname, file_object, read_hardware_id, read_id, read_record, FileObjectError,
    HardwareMemo, HardwareSource, IdFileStat, StdIdFileStat, LINUX_MACHINE_ID,
};
use super::{
    is_clone, new_machine_id, MachineFacts, MachineIdentity, ID_HEADER, ID_RANDOM_BYTES,
    MACHINE_FACTS_FILE, MACHINE_ID_FILE,
};
use crate::datetime::DateTime;
use crate::fsdata::{Fs, StdFs};
use crate::json::{self, DumpOptions};
use crate::location_probe::{Declined, StdHost};
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::store::create::owner_only_dir;
use crate::store::write::replace_retrying;

/// Why `open_machine` gives no identity.
#[derive(Debug)]
pub enum OpenError {
    /// An OSError the reference raises (the directory, a write).
    Os(io::Error),
    /// Another exception the reference raises.
    Raised(PyError),
    /// Not ported here.
    Declined(Declined),
}

impl From<io::Error> for OpenError {
    fn from(e: io::Error) -> Self {
        OpenError::Os(e)
    }
}

impl From<PyError> for OpenError {
    fn from(e: PyError) -> Self {
        OpenError::Raised(e)
    }
}

impl From<FileObjectError> for OpenError {
    fn from(e: FileObjectError) -> Self {
        match e {
            FileObjectError::Raised(e) => OpenError::Raised(e),
            FileObjectError::Declined(d) => OpenError::Declined(d),
        }
    }
}

fn cps(text: &str) -> PyStr {
    text.chars().map(u32::from).collect()
}

/// The bytes `_write_record` writes: `json.dumps(body, sort_keys=True,
/// indent=2) + "\n"` of the id, the facts, the predecessor and `now` as
/// `isoformat_utc`.
pub fn record_bytes(
    machine_id: &str,
    facts: &MachineFacts,
    predecessor: Option<&[u32]>,
    now: &DateTime,
) -> Result<Vec<u8>, PyError> {
    let body = PyValue::Dict(vec![
        (PyValue::str("id"), PyValue::str(machine_id)),
        (PyValue::str("facts"), facts.to_dict()),
        (
            PyValue::str("predecessor"),
            predecessor.map_or(PyValue::None, |p| PyValue::Str(p.to_vec())),
        ),
        (PyValue::str("recorded"), PyValue::str(&now.isoformat_utc())),
    ]);
    let text = json::dumps(&body, DumpOptions::DOCUMENT_INDENT2)?;
    let mut out = json::utf8_text(&text).into_bytes();
    out.push(b'\n');
    Ok(out)
}

/// `atomic_write_bytes(path, data, mode_before_rename=0o600)`.
fn write_private(path: &Path, data: &[u8]) -> io::Result<()> {
    replace_retrying(path, data)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// `_write_record(path, machine_id, facts, predecessor)` at `now`.
pub fn write_record(
    path: &Path,
    machine_id: &str,
    facts: &MachineFacts,
    predecessor: Option<&[u32]>,
    now: &DateTime,
) -> Result<(), OpenError> {
    let data = record_bytes(machine_id, facts, predecessor, now)?;
    Ok(write_private(path, &data)?)
}

#[cfg(unix)]
fn path_bytes(path: &Path) -> Option<Vec<u8>> {
    use std::os::unix::ffi::OsStrExt;
    Some(path.as_os_str().as_bytes().to_vec())
}

#[cfg(not(unix))]
fn path_bytes(_path: &Path) -> Option<Vec<u8>> {
    None
}

fn text_of(value: Option<&PyValue>) -> Option<PyStr> {
    match value {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

fn seen_facts<S: IdFileStat + ?Sized>(
    stat: &S,
    id_path: &[u8],
    hardware: &Option<PyStr>,
    hostname: &Option<PyStr>,
) -> Result<MachineFacts, OpenError> {
    let id_file =
        file_object(stat, id_path)?.map(|(d, i, b)| (cps(&d), cps(&i), b.map(|b| cps(&b))));
    Ok(MachineFacts {
        hardware: hardware.clone(),
        hostname: hostname.clone(),
        id_file,
    })
}

/// `open_machine(state_dir, hardware=..., hostname=...)` with
/// `datetime.now` answering `now` and `secrets.token_bytes(17)` answering
/// `urandom()`. The effects come in the reference's order: the directory,
/// the hardware read, the hostname read, the id file read, the facts file
/// read, then on a mint the id file written, its object read and the
/// facts file written.
#[allow(clippy::too_many_arguments)]
pub fn open_machine<F: Fs + ?Sized, S: IdFileStat + ?Sized>(
    state_dir: &Path,
    fs: &F,
    stat: &S,
    hardware: impl FnOnce() -> Result<Option<PyStr>, PyError>,
    hostname: impl FnOnce() -> Option<PyStr>,
    now: &DateTime,
    urandom: impl FnOnce() -> [u8; ID_RANDOM_BYTES],
) -> Result<MachineIdentity, OpenError> {
    let Some(dir) = path_bytes(state_dir) else {
        return Err(OpenError::Declined(Declined::NotPorted));
    };
    if dir.contains(&0) {
        return Err(OpenError::Raised(PyError::ValueError));
    }
    owner_only_dir(state_dir)?;
    let id_path = state_dir.join(MACHINE_ID_FILE);
    let facts_path = state_dir.join(MACHINE_FACTS_FILE);
    let id_bytes = path_bytes(&id_path).unwrap_or_default();
    let facts_bytes = path_bytes(&facts_path).unwrap_or_default();
    let seen_hardware = hardware()?;
    let seen_hostname = hostname();

    let current = read_id(fs, &id_bytes)?;
    let record = read_record(fs, &facts_bytes)?;
    // `if record`: dict truthiness, so `{}` reads as no record here.
    let full = matches!(&record, Some(PyValue::Dict(items)) if !items.is_empty());
    let (recorded, kept) = match &record {
        Some(r) if full => (
            MachineFacts::from_dict(r.get("facts").unwrap_or(&PyValue::None))?,
            text_of(r.get("predecessor")),
        ),
        _ => (None, None),
    };
    if let (Some(cur), Some(r), Some(old)) = (&current, &record, &recorded) {
        if text_of(r.get("id")).as_deref() == Some(cps(cur).as_slice()) {
            let seen = seen_facts(stat, &id_bytes, &seen_hardware, &seen_hostname)?;
            if !is_clone(old, &seen) {
                let updated = seen != *old;
                if updated {
                    write_record(&facts_path, cur, &seen, kept.as_deref(), now)?;
                }
                return Ok(MachineIdentity {
                    id: cps(cur),
                    facts: seen,
                    predecessor: kept,
                    minted: false,
                    cloned: false,
                    facts_updated: updated,
                    recorded: recorded.clone(),
                });
            }
        }
    }

    let minted = new_machine_id(&urandom());
    write_private(&id_path, format!("{ID_HEADER}\n{minted}\n").as_bytes())?;
    let facts = seen_facts(stat, &id_bytes, &seen_hardware, &seen_hostname)?;
    let predecessor = current.as_deref().map(cps);
    write_record(&facts_path, &minted, &facts, predecessor.as_deref(), now)?;
    Ok(MachineIdentity {
        id: cps(&minted),
        facts,
        predecessor,
        minted: true,
        cloned: current.is_some(),
        facts_updated: false,
        recorded,
    })
}

/// `sys.platform` as this build's reference would name it.
pub const PLATFORM: &str = if cfg!(target_os = "macos") {
    "darwin"
} else if cfg!(windows) {
    "win32"
} else if cfg!(target_os = "linux") {
    "linux"
} else {
    "other"
};

/// `ioreg`'s timeout (`subprocess.run(..., timeout=5)`).
pub const IOREG_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

/// `subprocess.run(..., text=True)`'s reading of `ioreg`'s output: UTF-8
/// (the locale's encoding on macOS), strict, then universal newlines.
/// Err(UnicodeDecodeError) where it does not decode, which the reference
/// does not catch.
pub fn ioreg_text(raw: &[u8]) -> Result<PyStr, PyError> {
    let text = std::str::from_utf8(raw).map_err(|_| PyError::UnicodeDecodeError)?;
    let mut out = Vec::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\r' {
            chars.next_if_eq(&'\n');
            out.push(u32::from('\n'));
        } else {
            out.push(u32::from(c));
        }
    }
    Ok(out)
}

/// The machine's hardware reads and its memo (`_HARDWARE`): `gethostuuid`
/// through `bettermemory_sys`, `ioreg` spawned with a five-second
/// timeout, MachineGuid not read (no safe registry API is in the
/// workspace, so [`StdMachine::hardware`] declines on Windows).
#[derive(Debug, Default)]
pub struct StdMachine {
    memo: Mutex<HardwareMemo>,
}

impl HardwareSource for StdMachine {
    fn gethostuuid(&self) -> Option<[u8; 16]> {
        bettermemory_sys::gethostuuid()
    }

    fn ioreg(&self) -> Result<Option<PyStr>, PyError> {
        use std::io::Read;
        use std::process::{Command, Stdio};
        let spawned = Command::new("ioreg")
            .args(["-rd1", "-c", "IOPlatformExpertDevice"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn();
        let Ok(mut child) = spawned else {
            return Ok(None);
        };
        let stdout = child.stdout.take();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            let read = stdout.map(|mut o| o.read_to_end(&mut buf));
            let _ = tx.send(matches!(read, Some(Ok(_))).then_some(buf));
        });
        match rx.recv_timeout(IOREG_TIMEOUT) {
            Ok(Some(buf)) => {
                let _ = child.wait();
                ioreg_text(&buf).map(Some)
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                Ok(None)
            }
        }
    }

    fn machine_guid(&self) -> Option<PyValue> {
        None
    }
}

impl StdMachine {
    /// `read_hardware_id()` on this machine, read at most once. Windows
    /// declines (`NotPorted`): MachineGuid is not read here.
    pub fn hardware(&self) -> Result<Option<PyStr>, OpenError> {
        if cfg!(windows) {
            return Err(OpenError::Declined(Declined::NotPorted));
        }
        let mut memo = self.memo.lock().unwrap_or_else(|e| e.into_inner());
        Ok(read_hardware_id(
            &mut memo,
            PLATFORM,
            self,
            &StdFs,
            &LINUX_MACHINE_ID,
        )?)
    }

    /// `open_machine(state_dir)` with this machine's facts.
    pub fn open(
        &self,
        state_dir: &Path,
        now: &DateTime,
        urandom: impl FnOnce() -> [u8; ID_RANDOM_BYTES],
    ) -> Result<MachineIdentity, OpenError> {
        open_machine(
            state_dir,
            &StdFs,
            &StdIdFileStat,
            // `open_machine` declines on Windows before this read, the
            // only place `hardware` declines.
            || match self.hardware() {
                Err(OpenError::Raised(e)) => Err(e),
                other => Ok(other.unwrap_or(None)),
            },
            || current_hostname(&StdHost),
            now,
            urandom,
        )
    }
}
