//! A request's caller as the reference's `identity` resolves it: the
//! declared-value check (`_clean`, `_strip_format_characters`), the JSON
//! workspace header (`_json_workspace`), the actor's selection rule and
//! record (`actor_matches`, `Actor.is_empty`, `Actor.to_record`), the
//! session bucket (`registry_key`), `Workspace.declared`, `resolve` over a
//! request given as data, and the published caller (`publish`, `reset`,
//! `current`, `bind`, `bind_transcript`, `current_actor`,
//! `current_harness_session`) as a value the caller owns.
//!
//! Effects are arguments: the environment is a lookup the caller passes
//! (read only off HTTP, as the reference reads it), the working directory
//! a function called only when no channel names the workspace, and the
//! roots the reference caches per connection in a weak table arrive as the
//! value that table holds for the request's session. Texts are code points
//! (a `str` may hold a lone surrogate, as `os.environ` gives one).

use crate::json;
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
