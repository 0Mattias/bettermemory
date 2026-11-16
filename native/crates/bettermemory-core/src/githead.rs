//! HEAD and the refs its chain names, read from a git directory's files
//! as `src/bettermemory/githead.py`'s resolution half reads them (lines
//! 692-1089 at v9's a983e3bb), with `gitfile_rule._read` (1083) as the
//! reader. No git is run and no file is read except through [`RefFs`].
//!
//! Every answer is the reference's: a value, or a decline (the reference's
//! None) where the files do not settle what git would answer and the
//! caller asks git. See `native/README.md`, "HEAD and refs from a git
//! directory's files".

pub mod fs;

pub use fs::{Flavour, MemFs, StdFs};

/// `gitfile_rule._REF_LIMIT`: the most bytes a ref file or `HEAD` holds.
pub const REF_LIMIT: usize = 1 << 16;
/// `githead._PACKED_LIMIT`: the most bytes `packed-refs` holds.
pub const PACKED_LIMIT: usize = 1 << 26;
/// `githead._MAX_READS`: git's `SYMREF_MAXDEPTH`, HEAD's read included.
pub const MAX_READS: usize = 5;
/// `githead._PEEL_DEPTH`: the tags `head_commit` peels through.
pub const PEEL_DEPTH: usize = 8;

/// `_SPACE`: what `_parse` strips.
const SPACE: &[u8] = b" \t\n\r";
/// `_PER_WORKTREE`: refs read under the git directory, not the common one.
const PER_WORKTREE: [&[u8]; 3] = [b"refs/bisect/", b"refs/rewritten/", b"refs/worktree/"];
const PACKED_HEADER: &[u8] = b"# pack-refs with:";

/// How a file-system call failed, as the reference tells failures apart.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FsError {
    /// `FileNotFoundError` that `path_missing` reads as nothing there.
    Missing,
    /// `NotADirectoryError` (ENOTDIR: a file where a directory of the
    /// path should be). Windows reports `Missing` there instead.
    NotADirectory,
    /// `IsADirectoryError`: the path is a directory.
    IsADirectory,
    /// Any other error: a symbolic link refused, a FIFO or device, a
    /// permission, a share that does not answer.
    Other,
}

/// The file system as the reader asks it. Paths are bytes joined with `/`.
pub trait RefFs {
    /// `gitfile_rule._read`: up to `limit + 1` leading bytes of the regular
    /// file at `path`, so a caller can tell a longer file apart. With
    /// `follow` false a symbolic link fails (`Other`) where the platform
    /// refuses it (POSIX's `O_NOFOLLOW`); a directory is `IsADirectory`,
    /// any other kind of file `Other`, and the call never blocks.
    fn read(&self, path: &[u8], limit: usize, follow: bool) -> Result<Vec<u8>, FsError>;
    /// `os.lstat(path)` read for whether it is a directory.
    fn lstat_is_dir(&self, path: &[u8]) -> Result<bool, FsError>;
}

/// `githead.GitDir` without `worktree_root`, which resolution never reads:
/// `gitdir` holds HEAD and the per-worktree refs, `commondir` the rest.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GitDir {
    pub gitdir: Vec<u8>,
    pub commondir: Vec<u8>,
}

/// What `head_commit` gives, short of the object reader this unit lacks.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HeadCommit {
    /// `head_sha` answered: the commit.
    Answer(String),
    /// HEAD ends at a ref outside `refs/heads/` holding this hash: the
    /// reference reads the object and peels tags (`_PEEL_DEPTH`).
    Peel(String),
    /// None: the caller asks git.
    Decline,
}

/// `_resolve`'s answer: (name, hash) on a branch, (name, None) unborn,
/// (None, hash) detached.
pub type Resolved = (Option<Vec<u8>>, Option<String>);

/// Why a step stopped: `_Unsettled`, or an `OSError` raised through.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stop {
    Unsettled,
    Os,
}

fn join(base: &[u8], name: &[u8]) -> Vec<u8> {
    let mut path = Vec::with_capacity(base.len() + 1 + name.len());
    path.extend_from_slice(base);
    path.push(b'/');
    path.extend_from_slice(name);
    path
}

fn rstrip_space(text: &[u8]) -> &[u8] {
    let end = text
        .iter()
        .rposition(|b| !SPACE.contains(b))
        .map_or(0, |i| i + 1);
    &text[..end]
}

fn lstrip_space(text: &[u8]) -> &[u8] {
    let start = text
        .iter()
        .position(|b| !SPACE.contains(b))
        .unwrap_or(text.len());
    &text[start..]
}

fn contains(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

/// `_FULL_HASH.fullmatch`: 40 or 64 lowercase hex digits.
pub fn full_hash(text: &[u8]) -> bool {
    (text.len() == 40 || text.len() == 64) && text.iter().all(|b| is_hex(*b))
}

fn is_hex(b: u8) -> bool {
    b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
}

/// `_BAD_REF_BYTES`, as a table.
const BAD_REF_BYTES: [bool; 256] = {
    let mut table = [false; 256];
    let mut i = 0;
    while i < 0x20 {
        table[i] = true;
        i += 1;
    }
    let marks = b" ~^:?*[\\\x7f";
    let mut j = 0;
    while j < marks.len() {
        table[marks[j] as usize] = true;
        j += 1;
    }
    table
};

fn bad_ref_byte(b: u8) -> bool {
    BAD_REF_BYTES[b as usize]
}

/// `_valid_refname` (705): git's `check_refname_format` as the reference
/// applies it.
pub fn valid_refname(name: &[u8]) -> bool {
    if contains(name, b"..") || contains(name, b"@{") || name.ends_with(b".") {
        return false;
    }
    if name.iter().any(|&b| bad_ref_byte(b)) {
        return false;
    }
    name.split(|&b| b == b'/')
        .all(|part| !part.is_empty() && !part.starts_with(b".") && !part.ends_with(b".lock"))
}

/// `_parse` (719): `(Some(name), None)` for `ref: <name>`, `(None,
/// Some(hash))` for a full hash, else a decline.
pub fn parse(content: &[u8]) -> Result<Resolved, Stop> {
    let text = rstrip_space(content);
    if let Some(rest) = text.strip_prefix(b"ref:") {
        let name = lstrip_space(rest);
        if name.starts_with(b"refs/") && valid_refname(name) {
            return Ok((Some(name.to_vec()), None));
        }
        return Err(Stop::Unsettled);
    }
    if full_hash(text) {
        return Ok((None, Some(ascii(text))));
    }
    Err(Stop::Unsettled)
}

fn ascii(text: &[u8]) -> String {
    text.iter().map(|&b| char::from(b)).collect()
}

/// `head_bytes` (692): HEAD's content, None on any error, a link, or more
/// than [`REF_LIMIT`] bytes.
pub fn head_bytes(fs: &dyn RefFs, gd: &GitDir) -> Option<Vec<u8>> {
    let content = fs.read(&join(&gd.gitdir, b"HEAD"), REF_LIMIT, false).ok()?;
    (content.len() <= REF_LIMIT).then_some(content)
}

fn per_worktree(name: &[u8]) -> bool {
    PER_WORKTREE.iter().any(|p| name.starts_with(p))
}

/// One parse of a `packed-refs` file of hashes `hex` digits long against
/// `_packed_shape(hex)`; the record names in order where it matches.
fn shape_names(data: &[u8], hex: usize) -> Option<Vec<&[u8]>> {
    let mut pos = 0;
    if data.starts_with(PACKED_HEADER) {
        pos = data.iter().position(|&b| b == b'\n')? + 1;
    }
    let mut names = Vec::new();
    let line_hash = |at: usize| -> bool {
        data.len() >= at + hex && data[at..at + hex].iter().all(|b| is_hex(*b))
    };
    while pos < data.len() {
        if !line_hash(pos) || data.get(pos + hex) != Some(&b' ') {
            return None;
        }
        let start = pos + hex + 1;
        let end = start
            + data[start..]
                .iter()
                .position(|&b| bad_ref_byte(b))
                .unwrap_or(data.len() - start);
        if end == start || data.get(end) != Some(&b'\n') {
            return None;
        }
        names.push(&data[start..end]);
        pos = end + 1;
        if data.get(pos) == Some(&b'^') {
            if !line_hash(pos + 1) || data.get(pos + 1 + hex) != Some(&b'\n') {
                return None;
            }
            pos += hex + 2;
        }
    }
    Some(names)
}

/// `_BAD_NAME` (758) searched over the whole file.
fn bad_name(data: &[u8]) -> bool {
    const BAD: [&[u8]; 9] = [
        b"..", b"@{", b"//", b"/.", b" .", b"/\n", b".lock/", b".lock\n", b".\n",
    ];
    // Every pattern starts with one of these bytes; the rest is checked
    // only there.
    data.iter().enumerate().any(|(i, &b)| {
        matches!(b, b'.' | b'@' | b'/' | b' ') && BAD.iter().any(|bad| data[i..].starts_with(bad))
    })
}

/// `_packed_verdict` (832): whether git reads the file line by line as
/// `_packed` does (the shape, the names, the order a `sorted` header claims).
pub fn packed_reads(data: &[u8]) -> bool {
    let Some(names) = shape_names(data, 40).or_else(|| shape_names(data, 64)) else {
        return false;
    };
    if bad_name(data) {
        return false;
    }
    if data.starts_with(PACKED_HEADER) {
        let end = data.iter().position(|&b| b == b'\n').unwrap_or(data.len());
        let sorted = data[PACKED_HEADER.len()..end]
            .split(|b| b" \t\n\r\x0b\x0c".contains(b))
            .any(|trait_| trait_ == b"sorted");
        if sorted {
            return names.windows(2).all(|w| w[0] < w[1]);
        }
    }
    true
}

/// Every start of `needle` in `hay`, in order, found as it is asked for
/// (Knuth-Morris-Pratt: one pass however long the name, and no position
/// held but the current one).
fn find_all<'a>(hay: &'a [u8], needle: &'a [u8]) -> impl Iterator<Item = usize> + 'a {
    let mut fail = vec![0usize; needle.len()];
    let mut k = 0;
    for i in 1..needle.len() {
        while k > 0 && needle[i] != needle[k] {
            k = fail[k - 1];
        }
        if needle[i] == needle[k] {
            k += 1;
        }
        fail[i] = k;
    }
    let mut k = 0;
    let usable = !needle.is_empty() && needle.len() <= hay.len();
    hay.iter()
        .enumerate()
        .take_while(move |_| usable)
        .filter_map(move |(i, &b)| {
            while k > 0 && b != needle[k] {
                k = fail[k - 1];
            }
            if b == needle[k] {
                k += 1;
            }
            if k == needle.len() {
                k = fail[k - 1];
                return Some(i + 1 - needle.len());
            }
            None
        })
}

/// `_packed` (855): the hash `packed-refs` records for `name`.
fn packed(fs: &dyn RefFs, commondir: &[u8], name: &[u8]) -> Result<Option<String>, Stop> {
    let data = match fs.read(&join(commondir, b"packed-refs"), PACKED_LIMIT, true) {
        Ok(data) => data,
        Err(FsError::Missing) => return Ok(None),
        Err(_) => return Err(Stop::Os),
    };
    if data.is_empty() {
        return Ok(None);
    }
    let header = data.starts_with(b"#");
    if data.len() > PACKED_LIMIT
        || !data.ends_with(b"\n")
        || (header && !data.starts_with(PACKED_HEADER))
        || !packed_reads(&data)
    {
        return Err(Stop::Unsettled);
    }
    let mut needle = Vec::with_capacity(name.len() + 2);
    needle.push(b' ');
    needle.extend_from_slice(name);
    needle.push(b'\n');
    let mut found = None;
    for at in find_all(&data, &needle) {
        let start = data[..at]
            .iter()
            .rposition(|&b| b == b'\n')
            .map_or(0, |i| i + 1);
        if header && start == 0 {
            continue;
        }
        let value = &data[start..at];
        if found.is_some() || !full_hash(value) {
            return Err(Stop::Unsettled);
        }
        found = Some(ascii(value));
    }
    Ok(found)
}

/// `_lookup` (909): what the ref `name` holds; the loose file wins.
pub fn lookup(fs: &dyn RefFs, gd: &GitDir, name: &[u8]) -> Result<Resolved, Stop> {
    let worktree = per_worktree(name);
    let base = if worktree { &gd.gitdir } else { &gd.commondir };
    match fs.read(&join(base, name), REF_LIMIT, false) {
        Ok(content) => {
            if content.len() > REF_LIMIT {
                return Err(Stop::Unsettled);
            }
            parse(&content)
        }
        Err(FsError::Missing | FsError::IsADirectory) => {
            if worktree {
                return Err(Stop::Unsettled);
            }
            Ok((None, packed(fs, &gd.commondir, name)?))
        }
        Err(_) => Err(Stop::Os),
    }
}

/// `_resolve` (932): HEAD followed to the end of its chain.
pub fn resolve(fs: &dyn RefFs, gd: &GitDir) -> Result<Resolved, Stop> {
    let head = head_bytes(fs, gd).ok_or(Stop::Unsettled)?;
    let (mut name, mut sha) = parse(&head)?;
    let mut reads = 1;
    while let Some(current) = name {
        if reads == MAX_READS {
            return Err(Stop::Unsettled);
        }
        reads += 1;
        let (target, found) = lookup(fs, gd, &current)?;
        sha = found;
        if target.is_none() {
            return Ok((Some(current), sha));
        }
        name = target;
    }
    Ok((None, sha))
}

/// `head_sha` (1022): the commit HEAD names, where it is detached or on a
/// branch under `refs/heads/`.
pub fn head_sha(fs: &dyn RefFs, gd: &GitDir) -> Option<String> {
    let (name, sha) = resolve(fs, gd).ok()?;
    if name.is_some_and(|n| !n.starts_with(b"refs/heads/")) {
        return None;
    }
    sha
}

/// `head_ref` (1080): the ref HEAD's chain ends at, as UTF-8.
pub fn head_ref(fs: &dyn RefFs, gd: &GitDir) -> Option<String> {
    let (name, _) = resolve(fs, gd).ok()?;
    String::from_utf8(name?).ok()
}

/// `head_commit` (1042) up to the object read: see [`HeadCommit`].
pub fn head_commit(fs: &dyn RefFs, gd: &GitDir) -> HeadCommit {
    if let Some(sha) = head_sha(fs, gd) {
        return HeadCommit::Answer(sha);
    }
    match resolve(fs, gd) {
        Ok((Some(_), Some(sha))) => HeadCommit::Peel(sha),
        _ => HeadCommit::Decline,
    }
}

/// `packed_names` (994): whether a line of `packed-refs` ends with a space
/// and `name`, a CR allowed before the line's end.
pub fn packed_names(fs: &dyn RefFs, gd: &GitDir, name: &[u8]) -> bool {
    let Ok(data) = fs.read(&join(&gd.commondir, b"packed-refs"), PACKED_LIMIT, true) else {
        return false;
    };
    let mut needle = Vec::with_capacity(name.len() + 1);
    needle.push(b' ');
    needle.extend_from_slice(name);
    let named = find_all(&data, &needle).any(|at| {
        let rest = &data[at + needle.len()..];
        let rest = rest
            .strip_prefix(b"\r")
            .filter(|r| r.is_empty() || r[0] == b'\n')
            .unwrap_or(rest);
        rest.is_empty() || rest[0] == b'\n'
    });
    named
}

/// `_loose_ref` (1011): a loose entry other than a directory stands for
/// `name`.
fn loose_ref(fs: &dyn RefFs, gd: &GitDir, name: &[u8]) -> bool {
    let base = if per_worktree(name) {
        &gd.gitdir
    } else {
        &gd.commondir
    };
    matches!(fs.lstat_is_dir(&join(base, name)), Ok(false))
}

/// `head_unborn` (952): Some(true) where HEAD's branch has no commit,
/// Some(false) where it has one or HEAD is detached, None where the files
/// do not settle it.
pub fn head_unborn(fs: &dyn RefFs, gd: &GitDir) -> Option<bool> {
    let head = head_bytes(fs, gd)?;
    let (mut name, _) = parse(&head).ok()?;
    let mut reads = 1;
    while let Some(current) = name {
        if reads == MAX_READS {
            return None;
        }
        reads += 1;
        match lookup(fs, gd, &current) {
            Err(Stop::Unsettled) => {
                if loose_ref(fs, gd, &current) || packed_names(fs, gd, &current) {
                    return Some(false);
                }
                return None;
            }
            Err(Stop::Os) => return None,
            Ok((None, sha)) => return Some(sha.is_none()),
            Ok((target, _)) => name = target,
        }
    }
    Some(false)
}
