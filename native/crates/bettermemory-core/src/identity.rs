//! A request's caller as the reference's `identity` resolves it: the
//! declared-value check (`_clean`, `_strip_format_characters`), the JSON
//! workspace header (`_json_workspace`), the actor's selection rule and
//! record (`actor_matches`, `Actor.is_empty`, `Actor.to_record`), the
//! session bucket (`registry_key`), `Workspace.declared`, `resolve` over a
//! request given as data, and the published caller (`publish`, `reset`,
//! `current`, `bind`, `bind_transcript`, `current_actor`,
//! `current_harness_session`) as a value the caller owns; the first
//! usable `file://` root (`_first_file_root`, over `urlparse` and
//! `unquote(errors="surrogateescape")`) and `workspace_declaration`.
//!
//! Effects are arguments: the environment is a lookup the caller passes
//! (read only off HTTP, as the reference reads it), the working directory
//! a function called only when no channel names the workspace, and the
//! roots the reference caches per connection in a weak table arrive as the
//! value that table holds for the request's session. Texts are code points
//! (a `str` may hold a lone surrogate, as `os.environ` gives one).

use crate::config::{self, Flavour, Fs, Raised};
use crate::json;
use crate::origin_remote::{self, UrlError};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode;

/// `identity.MAX_DECLARED_LEN`.
pub const MAX_DECLARED_LEN: usize = 256;
/// `identity._MAX_FORMAT_WALK_LEN`.
pub const MAX_FORMAT_WALK_LEN: usize = 4 * MAX_DECLARED_LEN;

pub const SOURCE_PRINCIPAL: &str = "principal";
pub const SOURCE_HEADER: &str = "header";
pub const SOURCE_ENV: &str = "env";
pub const SOURCE_CLIENT_INFO: &str = "client-info";
pub const SOURCE_TRANSPORT: &str = "transport";
pub const SOURCE_ROOTS: &str = "roots";
pub const SOURCE_PROCESS_CWD: &str = "process-cwd";
pub const SOURCE_TRANSCRIPT: &str = "transcript";

pub const HEADER_CLIENT: &str = "x-bettermemory-client";
pub const HEADER_CLIENT_VERSION: &str = "x-bettermemory-client-version";
pub const HEADER_MODEL: &str = "x-bettermemory-model";
pub const HEADER_WORKSPACE: &str = "x-bettermemory-workspace";
pub const HEADER_WORKSPACE_JSON: &str = "x-bettermemory-workspace-json";
pub const HEADER_MCP_SESSION: &str = "mcp-session-id";
pub const HEADER_SESSION: &str = "x-bettermemory-session";
pub const HEADER_HARNESS_SESSION: &str = "x-bettermemory-harness-session";

pub const ENV_CLIENT: &str = "BETTERMEMORY_CLIENT";
pub const ENV_CLIENT_VERSION: &str = "BETTERMEMORY_CLIENT_VERSION";
pub const ENV_MODEL: &str = "BETTERMEMORY_MODEL";
pub const ENV_WORKSPACE: &str = "BETTERMEMORY_WORKSPACE";
pub const ENV_HARNESS_SESSION: &str = "CLAUDE_CODE_SESSION_ID";

/// `bind_transcript`'s default client.
pub const TRANSCRIPT_CLIENT: &str = "claude-code";

fn cps(text: &str) -> PyStr {
    unicode::cps(text)
}

/// `str.isspace` of one code point, ASCII answered without the tables.
fn is_space(c: u32) -> bool {
    if c < 0x80 {
        return matches!(c, 0x09..=0x0D | 0x1C..=0x20);
    }
    unicode::is_space(c)
}

/// `str.strip()` with no argument: the `str.isspace` code points off both
/// ends.
fn py_strip(text: &[u32]) -> &[u32] {
    let start = text
        .iter()
        .position(|&c| !is_space(c))
        .unwrap_or(text.len());
    let end = text
        .iter()
        .rposition(|&c| !is_space(c))
        .map_or(start, |i| i + 1);
    &text[start..end]
}

/// `_strip_format_characters`: `text` without its category Cf code points.
pub fn strip_format_characters(text: &[u32]) -> PyStr {
    text.iter()
        .copied()
        .filter(|&c| unicode::category(c) != "Cf")
        .collect()
}

/// `_clean(value, strip_format=...)`. `value` is None for any value that is
/// not a `str` (None among them).
pub fn clean(value: Option<&[u32]>, strip_format: bool) -> Option<PyStr> {
    let stripped = py_strip(value?);
    // Both branches below refuse a value this long, so the non-ASCII scan
    // is skipped for it.
    if stripped.len() > MAX_FORMAT_WALK_LEN.max(MAX_DECLARED_LEN) {
        return None;
    }
    let text = if strip_format && stripped.iter().any(|&c| c >= 0x80) {
        if stripped.len() > MAX_FORMAT_WALK_LEN {
            return None;
        }
        py_strip(&strip_format_characters(stripped)).to_vec()
    } else if stripped.len() > MAX_DECLARED_LEN {
        return None;
    } else {
        stripped.to_vec()
    };
    if text.is_empty() || text.len() > MAX_DECLARED_LEN {
        return None;
    }
    let bad = |c: u32| (is_space(c) && c != 0x20) || c < 32 || c == 127;
    if text.iter().any(|&c| bad(c)) {
        return None;
    }
    Some(text)
}

/// `_json_workspace(value)`: `value` None for a value that is not a `str`.
pub fn json_workspace(value: Option<&[u32]>) -> Option<PyStr> {
    let value = value?;
    if value.len() > 6 * MAX_DECLARED_LEN + 2 {
        return None;
    }
    match json::loads(value) {
        Ok(PyValue::Str(ref path))
            if !path.is_empty() && path.len() <= MAX_DECLARED_LEN && !path.contains(&0) =>
        {
            Some(path.clone())
        }
        _ => None,
    }
}

/// `identity.Actor`. `sources` is the dict in its insertion order; a key
/// set again keeps its place ([`Actor::set_source`]).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Actor {
    pub client: Option<PyStr>,
    pub client_version: Option<PyStr>,
    pub model: Option<PyStr>,
    pub principal: Option<PyStr>,
    pub session: Option<PyStr>,
    pub sources: Vec<(PyStr, PyStr)>,
}

impl Actor {
    /// `sources.get(field)`.
    pub fn source(&self, field: &str) -> Option<&[u32]> {
        let key = cps(field);
        self.sources
            .iter()
            .find(|(k, _)| *k == key)
            .map(|(_, v)| v.as_slice())
    }

    /// `sources[field] = source`.
    pub fn set_source(&mut self, field: &str, source: &str) {
        let key = cps(field);
        match self.sources.iter_mut().find(|(k, _)| *k == key) {
            Some(entry) => entry.1 = cps(source),
            None => self.sources.push((key, cps(source))),
        }
    }

    /// `Actor.is_empty`.
    pub fn is_empty(&self) -> bool {
        self.client.is_none()
            && self.client_version.is_none()
            && self.model.is_none()
            && self.principal.is_none()
            && self.session.is_none()
    }

    /// `Actor.to_record`: the set fields in declaration order, `sources`
    /// last and only when non-empty.
    pub fn to_record(&self) -> PyValue {
        let mut out = Vec::new();
        let fields = [
            ("client", &self.client),
            ("client_version", &self.client_version),
            ("model", &self.model),
            ("principal", &self.principal),
            ("session", &self.session),
        ];
        for (name, value) in fields {
            if let Some(v) = value {
                out.push((PyValue::str(name), PyValue::Str(v.clone())));
            }
        }
        if !self.sources.is_empty() {
            let sources = self
                .sources
                .iter()
                .map(|(k, v)| (PyValue::Str(k.clone()), PyValue::Str(v.clone())))
                .collect();
            out.push((PyValue::str("sources"), PyValue::Dict(sources)));
        }
        PyValue::Dict(out)
    }
}

/// `actor_matches(actor, client=..., model=...)`.
pub fn actor_matches(actor: Option<&Actor>, client: Option<&[u32]>, model: Option<&[u32]>) -> bool {
    if client.is_none() && model.is_none() {
        return true;
    }
    let Some(actor) = actor else {
        return false;
    };
    if client.is_some() && actor.client.as_deref() != client {
        return false;
    }
    if model.is_some() && actor.model.as_deref() != model {
        return false;
    }
    true
}

/// `registry_key(actor)`: the parts joined by `|`, None when there are none.
pub fn registry_key(actor: &Actor) -> Option<PyStr> {
    let mut parts: Vec<PyStr> = Vec::new();
    let mut part = |name: &str, value: &[u32]| {
        let mut p = cps(name);
        p.push(u32::from('='));
        p.extend_from_slice(value);
        parts.push(p);
    };
    let header = cps(SOURCE_HEADER);
    let transport = cps(SOURCE_TRANSPORT);
    if let Some(p) = &actor.principal {
        part("principal", p);
    }
    if let Some(s) = &actor.session {
        if let Some(src) = actor.source("session") {
            if src == transport.as_slice() || src == header.as_slice() {
                part("session", s);
            }
        }
    }
    if let Some(c) = &actor.client {
        if actor.source("client") == Some(header.as_slice()) {
            part("client", c);
        }
    }
    if let Some(m) = &actor.model {
        if actor.source("model") == Some(header.as_slice()) {
            part("model", m);
        }
    }
    if parts.is_empty() {
        return None;
    }
    Some(parts.join(&u32::from('|')))
}

/// `identity.Workspace`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Workspace {
    pub path: Option<PyStr>,
    pub source: PyStr,
}

impl Default for Workspace {
    fn default() -> Self {
        Workspace {
            path: None,
            source: cps(SOURCE_PROCESS_CWD),
        }
    }
}

impl Workspace {
    fn named(path: PyStr, source: &str) -> Workspace {
        Workspace {
            path: Some(path),
            source: cps(source),
        }
    }

    /// `Workspace.declared`.
    pub fn declared(&self) -> bool {
        self.source != cps(SOURCE_PROCESS_CWD) && self.path.is_some()
    }
}

/// `identity.Caller`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Caller {
    pub actor: Actor,
    pub workspace: Workspace,
    pub request_id: Option<PyStr>,
    pub harness_session: Option<PyStr>,
}

/// A request context as `resolve` reads it, through the attribute paths
/// the reference follows.
#[derive(Clone, Debug, Default)]
pub struct Request {
    /// `request.headers.items()`, the pairs whose key and value are both a
    /// `str`, in order; None when `request.headers` is missing or None
    /// (not HTTP). Some(empty) is HTTP with no usable header, as is a
    /// headers object without `items`.
    pub headers: Option<Vec<(PyStr, PyStr)>>,
    /// `session.client_params.client_info`: None when it is missing or
    /// None; else its `name` and `version`, each None when it is not a
    /// `str`.
    pub client_info: Option<(Option<PyStr>, Option<PyStr>)>,
    /// What `authenticated_principal` returned when it was a `str`; None
    /// when it returned anything else or raised.
    pub principal: Option<PyStr>,
    /// `str(request_id)`, None when the id is None.
    pub request_id: Option<PyStr>,
    /// The roots table's entry for the request's session: None when it
    /// holds none, holds None, or there is no session to key it by.
    pub root: Option<PyStr>,
}

/// `_headers_of` over pairs whose keys `resolve` lower-cased once by
/// `str.lower`, a later pair replacing an earlier one with the same key.
fn header<'a>(headers: &[(PyStr, &'a [u32])], name: &str) -> Option<&'a [u32]> {
    let name = cps(name);
    headers
        .iter()
        .rev()
        .find(|(k, _)| *k == name)
        .map(|(_, v)| *v)
}

/// `resolve(request_context, environ=...)`. `env` answers a key's value
/// (`environ.get`); it is never called for a request over HTTP. `cwd`
/// answers `str(Path.cwd())`, or None when it raises; it is called only
/// when no channel names the workspace.
pub fn resolve(
    request: Option<&Request>,
    env: &mut dyn FnMut(&str) -> Option<PyStr>,
    cwd: &mut dyn FnMut() -> Option<PyStr>,
) -> Caller {
    let none = Vec::new();
    let over_http = request.is_some_and(|r| r.headers.is_some());
    let raw: &[(PyStr, PyStr)] = request.and_then(|r| r.headers.as_ref()).unwrap_or(&none);
    let lowered: Vec<(PyStr, &[u32])> = raw
        .iter()
        .map(|(k, v)| (unicode::lower(k), v.as_slice()))
        .collect();
    let headers = lowered.as_slice();
    let mut env_get = |key: &str| if over_http { None } else { env(key) };
    let mut actor = Actor::default();

    let mut declared = |actor: &mut Actor, field: &str, h: &str, e: &str| {
        if let Some(v) = clean(header(headers, h), true) {
            actor.set_source(field, SOURCE_HEADER);
            return Some(v);
        }
        if let Some(v) = clean(env_get(e).as_deref(), true) {
            actor.set_source(field, SOURCE_ENV);
            return Some(v);
        }
        None
    };
    actor.client = declared(&mut actor, "client", HEADER_CLIENT, ENV_CLIENT);
    actor.client_version = declared(
        &mut actor,
        "client_version",
        HEADER_CLIENT_VERSION,
        ENV_CLIENT_VERSION,
    );
    if actor.client.is_none() || actor.client_version.is_none() {
        let (name, version) = match request.and_then(|r| r.client_info.as_ref()) {
            Some((n, v)) => (clean(n.as_deref(), true), clean(v.as_deref(), true)),
            None => (None, None),
        };
        if actor.client.is_none() && name.is_some() {
            actor.client = name;
            actor.set_source("client", SOURCE_CLIENT_INFO);
        }
        if actor.client_version.is_none() && version.is_some() {
            actor.client_version = version;
            actor.set_source("client_version", SOURCE_CLIENT_INFO);
        }
    }
    actor.model = declared(&mut actor, "model", HEADER_MODEL, ENV_MODEL);

    if let Some(s) = clean(header(headers, HEADER_MCP_SESSION), true) {
        actor.session = Some(s);
        actor.set_source("session", SOURCE_TRANSPORT);
    } else if let Some(s) = clean(header(headers, HEADER_SESSION), true) {
        actor.session = Some(s);
        actor.set_source("session", SOURCE_HEADER);
    }
    actor.principal = request.and_then(|r| clean(r.principal.as_deref(), false));

    let workspace = if let Some(p) = clean(header(headers, HEADER_WORKSPACE), false)
        .or_else(|| json_workspace(header(headers, HEADER_WORKSPACE_JSON)))
    {
        Workspace::named(p, SOURCE_HEADER)
    } else if let Some(p) = clean(env_get(ENV_WORKSPACE).as_deref(), false) {
        Workspace::named(p, SOURCE_ENV)
    } else if let Some(root) = request.and_then(|r| r.root.clone()) {
        Workspace::named(root, SOURCE_ROOTS)
    } else {
        Workspace {
            path: cwd(),
            source: cps(SOURCE_PROCESS_CWD),
        }
    };

    let mut harness_session = clean(header(headers, HEADER_HARNESS_SESSION), true);
    if harness_session.is_none() && !over_http {
        harness_session = clean(env_get(ENV_HARNESS_SESSION).as_deref(), true);
    }
    Caller {
        actor,
        workspace,
        request_id: request.and_then(|r| r.request_id.clone()),
        harness_session,
    }
}

/// What `publish` hands back: the caller published before, which `reset`
/// restores. It is consumed by `reset`, so it cannot be used twice.
#[derive(Debug)]
pub struct Token {
    previous: Option<Caller>,
}

/// The reference's `_CURRENT` context variable for one task, owned by the
/// caller.
#[derive(Clone, Debug, Default)]
pub struct Published {
    current: Option<Caller>,
}

impl Published {
    /// `current()`.
    pub fn current(&self) -> Option<&Caller> {
        self.current.as_ref()
    }

    /// `publish(caller)`.
    pub fn publish(&mut self, caller: Caller) -> Token {
        Token {
            previous: self.current.replace(caller),
        }
    }

    /// `reset(token)`.
    pub fn reset(&mut self, token: Token) {
        self.current = token.previous;
    }

    /// `bind(ctx)`: `request` is `ctx.request_context`, None when there is
    /// no context or reading it raises.
    pub fn bind(
        &mut self,
        request: Option<&Request>,
        env: &mut dyn FnMut(&str) -> Option<PyStr>,
        cwd: &mut dyn FnMut() -> Option<PyStr>,
    ) -> Caller {
        let caller = resolve(request, env, cwd);
        self.publish(caller.clone());
        caller
    }

    /// `bind_transcript(session_id=..., model=..., client=...)`; the
    /// reference's default client is [`TRANSCRIPT_CLIENT`].
    pub fn bind_transcript(
        &mut self,
        session_id: Option<&[u32]>,
        model: Option<&[u32]>,
        client: Option<&[u32]>,
        env: &mut dyn FnMut(&str) -> Option<PyStr>,
        cwd: &mut dyn FnMut() -> Option<PyStr>,
    ) -> Caller {
        let mut actor = Actor {
            client: clean(client, true),
            model: clean(model, true),
            session: clean(session_id, true),
            ..Actor::default()
        };
        for (name, set) in [
            ("client", actor.client.is_some()),
            ("model", actor.model.is_some()),
            ("session", actor.session.is_some()),
        ] {
            if set {
                actor.set_source(name, SOURCE_TRANSCRIPT);
            }
        }
        let workspace = resolve(None, env, cwd).workspace;
        let caller = Caller {
            actor,
            workspace,
            ..Caller::default()
        };
        self.publish(caller.clone());
        caller
    }

    /// `current_actor()`.
    pub fn current_actor(&self) -> Option<&Actor> {
        self.current
            .as_ref()
            .map(|c| &c.actor)
            .filter(|a| !a.is_empty())
    }

    /// `current_harness_session()`.
    pub fn current_harness_session(&self) -> Option<&[u32]> {
        self.current.as_ref()?.harness_session.as_deref()
    }
}

/// `urllib.parse.unquote(text, errors="surrogateescape")`: each maximal
/// run of ASCII code points has its `%XX` escapes decoded to bytes (a `%`
/// not followed by two hex digits is kept) and is decoded as UTF-8, each
/// byte of an invalid sequence becoming U+DC00 plus the byte
/// (`surrogateescape`); a code point past
/// ASCII passes through as it is, a lone surrogate included.
pub fn unquote(text: &[u32]) -> PyStr {
    // The reference returns text holding no `%` as it is.
    if !text.contains(&u32::from('%')) {
        return text.to_vec();
    }
    let mut out = Vec::with_capacity(text.len());
    let mut i = 0;
    while i < text.len() {
        if text[i] >= 0x80 {
            out.push(text[i]);
            i += 1;
            continue;
        }
        let end = i + text[i..].iter().take_while(|&&c| c < 0x80).count();
        decode_surrogateescape(&unquote_to_bytes(&text[i..end]), &mut out);
        i = end;
    }
    out
}

/// `unquote_to_bytes` of a run of ASCII code points.
fn unquote_to_bytes(run: &[u32]) -> Vec<u8> {
    let hex = |c: Option<&u32>| c.and_then(|&c| char::from_u32(c)?.to_digit(16));
    let mut out = Vec::with_capacity(run.len());
    let mut i = 0;
    while i < run.len() {
        if run[i] == u32::from('%') {
            if let (Some(h), Some(l)) = (hex(run.get(i + 1)), hex(run.get(i + 2))) {
                out.push((h * 16 + l) as u8);
                i += 3;
                continue;
            }
        }
        out.push(run[i] as u8);
        i += 1;
    }
    out
}

/// `bytes.decode("utf-8", "surrogateescape")`: Python's decoder reports
/// the maximal invalid subpart, as `from_utf8` does, and every byte of it
/// is escaped.
fn decode_surrogateescape(mut bytes: &[u8], out: &mut Vec<u32>) {
    loop {
        match std::str::from_utf8(bytes) {
            Ok(text) => {
                out.extend(text.chars().map(u32::from));
                return;
            }
            Err(e) => {
                let (good, rest) = bytes.split_at(e.valid_up_to());
                out.extend(
                    std::str::from_utf8(good)
                        .unwrap_or_default()
                        .chars()
                        .map(u32::from),
                );
                let bad = e.error_len().unwrap_or(rest.len());
                out.extend(rest[..bad].iter().map(|&b| 0xDC00 + u32::from(b)));
                bytes = &rest[bad..];
            }
        }
    }
}

/// The netloc `urlsplit` reads from `url`, before any check: the leading
/// C0 controls and spaces stripped, tab, CR and LF removed, a valid
/// scheme taken off, then what follows `//` up to `/`, `?` or `#`.
fn netloc_of(url: &[u32]) -> PyStr {
    let lead = url.iter().take_while(|&&c| c <= 0x20).count();
    let url: PyStr = url[lead..]
        .iter()
        .copied()
        .filter(|&c| !matches!(c, 0x09 | 0x0A | 0x0D))
        .collect();
    let ascii = |c: u32, more: &str| {
        c < 0x80 && ((c as u8).is_ascii_alphanumeric() || more.contains(c as u8 as char))
    };
    let mut rest = url.as_slice();
    if let Some(i) = url.iter().position(|&c| c == u32::from(':')) {
        if i > 0
            && url[0] < 0x80
            && (url[0] as u8).is_ascii_alphabetic()
            && url[..i].iter().all(|&c| ascii(c, "+-."))
        {
            rest = &url[i + 1..];
        }
    }
    match rest.strip_prefix(&[0x2F, 0x2F][..]) {
        Some(after) => after
            .iter()
            .take_while(|&&c| !matches!(c, 0x2F | 0x3F | 0x23))
            .copied()
            .collect(),
        None => PyStr::new(),
    }
}

/// Lone surrogates written as `\udcXX` (`backslashreplace`), since a
/// `Raised` message is Rust text.
fn message_text(text: &[u32]) -> String {
    text.iter()
        .map(|&c| char::from_u32(c).map_or_else(|| format!("\\u{c:04x}"), String::from))
        .collect()
}

/// The ValueError `urlparse(url)` raised, with CPython 3.13's message.
fn url_raised(error: UrlError, url: &[u32]) -> Raised {
    let netloc = netloc_of(url);
    let message = match error {
        UrlError::Ipv6 => "Invalid IPv6 URL".to_owned(),
        UrlError::Future => "IPvFuture address is invalid".to_owned(),
        UrlError::Ipv4 => "An IPv4 address cannot be in brackets".to_owned(),
        UrlError::Address => {
            // `_check_bracketed_netloc`'s split: after the last `@`, then
            // between `[` and `]`, or before the first `:` with no `[`.
            let at = netloc.iter().rposition(|&c| c == u32::from('@'));
            let host_port = &netloc[at.map_or(0, |i| i + 1)..];
            let upto = |text: &'_ [u32], end: char| {
                let stop = text.iter().position(|&c| c == u32::from(end));
                text[..stop.unwrap_or(text.len())].to_vec()
            };
            let host = match host_port.iter().position(|&c| c == u32::from('[')) {
                Some(i) => upto(&host_port[i + 1..], ']'),
                None => upto(host_port, ':'),
            };
            format!(
                "{} does not appear to be an IPv4 or IPv6 address",
                message_text(&crate::pyvalue::repr_str(&host))
            )
        }
        UrlError::Nfkc => format!(
            "netloc '{}' contains invalid characters under NFKC normalization",
            message_text(&netloc)
        ),
    };
    Raised::value(message)
}

/// `_first_file_root(roots)`: each root is `str(getattr(root, "uri", "")
/// or "")`, None standing for the empty text. The ValueError `urlparse`
/// raises is not caught, so a malformed URI ahead of a usable one raises.
pub fn first_file_root(roots: &[Option<PyStr>]) -> Result<Option<PyStr>, Raised> {
    let file = cps("file");
    let localhost = cps("localhost");
    for root in roots {
        let uri = root.as_deref().unwrap_or(&[]);
        let parsed = origin_remote::urlparse(uri).map_err(|e| url_raised(e, uri))?;
        if parsed.scheme != file {
            continue;
        }
        let mut path = unquote(&parsed.path);
        if path.len() >= 3 && path[0] == u32::from('/') && path[2] == u32::from(':') {
            path.remove(0);
        }
        if !parsed.netloc.is_empty() && parsed.netloc != localhost {
            let mut unc = cps("//");
            unc.extend_from_slice(&parsed.netloc);
            unc.extend_from_slice(&path);
            path = unc;
        }
        if let Some(cleaned) = clean(Some(&path), false) {
            return Ok(Some(cleaned));
        }
    }
    Ok(None)
}

/// `workspace_declaration()`: `(str(Path(path).expanduser()), source)`
/// when the published caller's workspace (or, with none published,
/// `resolve(None)`'s) is declared. `flavour` is the `Path` of the host,
/// and `fs.expanduser` its `expanduser`, which raises
/// [`config::no_home`] where the reference raises RuntimeError. A
/// declared path holding a lone surrogate cannot be spelled as Rust text:
/// it is declined with a `NotPorted` raise.
pub fn workspace_declaration(
    published: &Published,
    env: &mut dyn FnMut(&str) -> Option<PyStr>,
    cwd: &mut dyn FnMut() -> Option<PyStr>,
    fs: &dyn Fs,
    flavour: Flavour,
) -> Result<Option<(PyStr, PyStr)>, Raised> {
    let resolved;
    let workspace = match published.current() {
        Some(caller) => &caller.workspace,
        None => {
            resolved = resolve(None, env, cwd).workspace;
            &resolved
        }
    };
    let Some(path) = workspace.path.as_deref().filter(|_| workspace.declared()) else {
        return Ok(None);
    };
    let text: Option<String> = path.iter().map(|&c| char::from_u32(c)).collect();
    let Some(text) = text else {
        return Err(Raised {
            class: "NotPorted",
            message: "a declared workspace holding a lone surrogate".to_owned(),
        });
    };
    let expanded = fs.expanduser(&config::pure_path(&text, flavour))?;
    Ok(Some((cps(&expanded), workspace.source.clone())))
}
