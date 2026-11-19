"""Fixtures: the remote URL parser of `origin_remote`
(``origin_remote.jsonl``, ``origin_remote_unicode.jsonl``,
``origin_remote_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/origin_remote.rs``. This pins it to
`origin_remote`'s `_parse_remote`, `_canonicalize` and
`_canonicalize_azure`, and to `urllib.parse.urlparse` (with its
``hostname``) on every URL `_parse_remote` hands it.

Inputs are built by rule: scp-like remotes (user prefixes, hosts and the
text after the colon crossed), URL-form remotes (schemes, netlocs and
paths crossed, vendor hosts with vendor paths), whitespace and control
characters at each end and inside, and triples handed to the two
canonicalizers directly. A text is a JSON string, or a list of code
points where it holds a lone surrogate.

The first line is a header (each kind's count). Then:

- ``["p", url, triple|null]``: `_parse_remote(url)`;
- ``["u", url, parts|kind]``: `urlparse(url)` as ``[scheme, netloc,
  path, params, query, fragment, hostname]`` (hostname null where
  ``None``), or the ValueError it raised, by kind: ``"ipv6"`` (Invalid
  IPv6 URL), ``"future"`` (IPvFuture address is invalid), ``"ipv4"`` (an
  IPv4 address in brackets), ``"address"`` (neither address),
  ``"nfkc"`` (characters NFKC turns into a delimiter);
- ``["c", triple, triple]``: `_canonicalize`;
- ``["z", triple, triple|null]``: `_canonicalize_azure`.

Gates. A row whose inputs hold a code point past ASCII goes into
``_unicode``: `str.strip`, `str.lower` and NFKC read the Unicode data. A
row whose URL holds a bracket goes into ``_python``: the bracketed-host
checks of `urlsplit` and `ipaddress`'s grammar are the release's (3.11
before 3.11.4 checks no bracketed host). Every other row is the same
under CPython 3.11.15, 3.12.13 and 3.13.13 (checked).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

from bettermemory import origin_remote

from ._common import jsonl

_Triple = tuple[str, str, str]


def _s(text: str) -> str | list[int]:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        return [ord(c) for c in text]
    return text


def _t(triple: _Triple | None) -> list[Any] | None:
    return None if triple is None else [_s(x) for x in triple]


_USERS_ODD = ["@", "git@@", "g t@", "g/t@", "g:t@", "\u00e9@"]
_SCP_HOSTS = [
    "github.com",
    "ab",
    "a",
    "SSH.GitHub.com",
    "ssh.github.com",
    "altssh.gitlab.com",
    "ssh.dev.azure.com",
    "vs-ssh.visualstudio.com",
    "x.visualstudio.com",
    "dev.azure.com",
    "h\nx",
    "h x",
    "C",
    "h.",
]
_SCP_TAILS = [
    "owner/name.git",
    "owner/name",
    "/owner/name.git",
    "//owner/name",
    "/",
    "",
    "name",
    "name.git",
    "name.git/",
    "name/",
    ".git",
    ".git/",
    "/.git",
    "o/.git",
    "a/b/c",
    "a/b/c.git/",
    "v3/org/proj/repo",
    "V3/org/proj/repo",
    "v3/org/proj",
    "v3/org/proj/repo/x",
    "v3/o//r",
    "v3//p/r",
    "owner/",
    "owner//",
    "owner//name",
    "o/n/",
    "o/n//",
    "a.git.git",
    "o/n.git//",
    "o/n.git.git/",
    "o\n/n",
    "o/n\nx",
    "o/n\n",
    "\no/n",
    "o:/n",
    "o@x/n",
    "///",
    "/o/",
    "x.gi",
    "x.GIT",
    "x/.git/",
    "_git/x",
]
_SCHEMES = ["http://", "https://", "git://", "ssh://", "git+ssh://", "ssh+git://"]
_NETLOCS = [
    "github.com",
    "GitHub.COM",
    "git@github.com",
    "u:p@h:22",
    "h:",
    ":22",
    "@h",
    "a@b@h",
    "h:x:y",
    "",
    "ssh.github.com",
    "SSH.github.com:443",
    "h%41",
    "h%",
]
_BRACKETS = [
    "[::1]",
    "[::1]:22",
    "[::1]x",
    "x[::1]",
    "[::1",
    "::1]",
    "]::1[",
    "[v1.x]",
    "[V1.x]",
    "[v1.]",
    "[vg.x]",
    "[v.x]",
    "[1.2.3.4]",
    "[x]",
    "[]",
    "[fe80::1%eth0]",
    "[FE80::A%Eth0]",
    "[fe80::1%]",
    "[fe80::1%a%b]",
    "[::ffff:1.2.3.4]",
    "[::1.2.3.04]",
    "[::1.2.3]",
    "[1:2:3:4:5:6:7:8]",
    "[1:2:3:4:5:6:7:8:9]",
    "[1::2::3]",
    "[:1::]",
    "[1:2:3:4:5:6:7::]",
    "[::1:2:3:4:5:6:7]",
    "[1:2:3:4:5:6:7]",
    "[12345::]",
    "[g::]",
    "[" + "0:" * 7 + "0000]",
    "[" + "0" * 46 + "::]",
    "u[x]@h",
    "u@[::1]",
    "[::1]@h",
    "a@[[::1]]",
    "[::1]:x",
    "[::1]]",
    "[1:2:3:4:5:6:1.2.3.4]",
]
_PATHS = [
    "/o/n.git",
    "/o/n",
    "/n",
    "/n.git",
    "/",
    "",
    "//",
    "/.git",
    "/o/n.git/",
    "/o/n/",
    "/o//n",
    "/a/b/c",
    "/o/n?x#y",
    "/o/n#f?q",
    "?q/o/n",
    "#/o/n",
    "/o/n;p",
    "/o/n;p/q",
    "/o;x/n",
    ";p",
    "/o/n.git.git",
    "/o/n%2F",
]
_VENDOR_HOSTS = [
    "bitbucket.example.com",
    "BitBucket.example.com",
    "gerrit.example.com",
    "Gerrit.x",
    "gitlab.com",
    "dev.azure.com",
    "org@dev.azure.com",
    "foo.visualstudio.com",
    "FOO.VisualStudio.com",
    "a.b.visualstudio.com",
    ".visualstudio.com",
    "visualstudio.com",
]
_VENDOR_PATHS = [
    "/scm/team/proj.git",
    "/SCM/team/proj",
    "/scm/proj",
    "/scm/t/p/q",
    "/a/team/proj",
    "/A/team/proj",
    "/a/proj",
    "/a/b",
    "/org/proj/_git/repo",
    "/org/proj/_git/repo.git",
    "/org/proj/_GIT/repo",
    "/org//_git/repo",
    "/org/proj/_git/",
    "/proj/_git/repo",
    "/DefaultCollection/proj/_git/repo",
    "/defaultcollection/proj/_git/repo",
    "/DefaultCollection/_git/repo",
    "/org/proj/repo",
    "/_git/repo",
]
_WHOLE = [
    "",
    " ",
    "\t\n\r\x0b\x0c\x1c\x1d\x1e\x1f",
    "  git@h:o/n  ",
    "\ngit@h:o/n\n",
    "\x1cgit@h:o/n\x1f",
    "\x00git@h:o/n",
    "git@h:o/n\x00",
    "\x85git@h:o/n\xa0",
    "\u3000git@h:o/n\u2028",
    "\u200bgit@h:o/n",
    " https://github.com/o/n ",
    "\x00https://github.com/o/n",
    "https://git\thub.com/o/n",
    "https://git\rhub.com/o\n/n",
    "https://github.com/o/n\x00",
    "https://github.com/o\t/n",
    "https:\t//h/o/n",
    "HTTPS://github.com/o/n",
    "Https://github.com/o/n",
    "ftp://h/o/n",
    "file:///o/n",
    "https:/h/o/n",
    "https:h/o/n",
    "https://",
    "https:///o/n",
    "https://h",
    "https://h?x/o/n",
    "https://h#x/o/n",
    "./local/path:odd",
    "../a:b/c",
    "C:/U/x/repo",
    "C:\\U\\x\\repo",
    "/abs/path",
    "file:o/n",
    "host:o/n",
    "user@host:o/n",
    "@host:o/n",
    "host:",
    "host::",
    "host://o/n",
    "host:/o/n",
    "host:/",
    "git@[::1]:o/n",
    "unknown://h/o/n",
    "git@host/o:n",
    "h\u00f4st:o/n",
    "git@h:\u00e9/n",
    "https://\u0130.com/o/n",
    "https://\u03a3\u0391.com/o/n",
    "https://a\u2100b/o/n",
    "https://\uff47ithub.com/o/n",
    "https://\uff0fx/o/n",
    "https://a\ufe6bb/o/n",
    "https://h\u00e9/o/n",
    "https://u\u00e9@h/o/n",
    "https://h/\u00e9/n",
    "https://h:\u0661/o/n",
    "https://[\u0661::1]/o/n",
    "https://\ud800/o/n",
    "git@\ud800h:o/n",
    "https://h/o/\udcff",
    "https://\u212a.com/o/n",
    "https://scm.bitbucket.\u0130/scm/t/p",
    "https://bitbucket.x/\u0130/t/p",
    "https://\u212aitbucket.x/scm/t/p",
    "https://GERRIT\u0130.x/a/t/p",
    "https://h/o/n\u3000",
    "\u3000https://h/o/n",
]


def _urls() -> Iterator[str]:
    for host in _SCP_HOSTS:
        for tail in _SCP_TAILS:
            yield f"git@{host}:{tail}"
    for user in ("", "a.B+c-9_z@"):
        for host in ("github.com", "a", "ssh.dev.azure.com", "h\nx"):
            for tail in _SCP_TAILS:
                yield f"{user}{host}:{tail}"
    for user in _USERS_ODD:
        for tail in ("o/n.git", "n", ""):
            yield f"{user}github.com:{tail}"
    for netloc in _NETLOCS:
        for path in _PATHS:
            yield "https://" + netloc + path
    for scheme in _SCHEMES:
        for netloc in _NETLOCS:
            for path in ("/o/n.git", "/n", "", "/o/n;p"):
                yield scheme + netloc + path
        if scheme in ("https://", "ssh://"):
            for netloc in _BRACKETS:
                for path in ("/o/n.git", ""):
                    yield scheme + netloc + path
    for host in _VENDOR_HOSTS:
        for path in _VENDOR_PATHS:
            yield "https://" + host + path
        for scheme in ("http://", "ssh://"):
            for path in _VENDOR_PATHS[::3]:
                yield scheme + host + path
    for scheme in ("git://", "git+ssh://"):
        for path in _VENDOR_PATHS[:8]:
            yield scheme + "bitbucket.x" + path
            yield scheme + "gerrit.x" + path
    yield from _WHOLE


def _triples() -> Iterator[_Triple]:
    hosts = [
        "github.com",
        "ssh.github.com",
        "SSH.GITHUB.COM",
        "altssh.gitlab.com",
        "AltSSH.GitLab.com",
        "ssh.github.com.",
        "dev.azure.com",
        "Dev.Azure.com",
        "ssh.dev.azure.com",
        "SSH.dev.azure.com",
        "vs-ssh.visualstudio.com",
        "foo.visualstudio.com",
        "Foo.VisualStudio.com",
        ".visualstudio.com",
        "a.b.visualstudio.com",
        "visualstudio.com",
        "x.visualstudio.comx",
        "\u0130.visualstudio.com",
        "\u212a.visualstudio.com",
        "",
    ]
    pairs = [
        ("o", "n"),
        ("", "n"),
        ("v3", "org/proj/repo"),
        ("V3", "org/proj/repo"),
        ("v3", "org/proj"),
        ("v3", "org//repo"),
        ("v3", "org/proj/repo/x"),
        ("v3", "/proj/repo"),
        ("org", "proj/_git/repo"),
        ("", "proj/_git/repo"),
        ("org", "/_git/repo"),
        ("org", "proj/_git/"),
        ("org", "proj/_GIT/repo"),
        ("org", "proj/_git/repo/x"),
        ("DefaultCollection", "proj/_git/repo"),
        ("", "DefaultCollection/proj/_git/repo"),
        ("DefaultCollection", "_git/repo"),
        ("proj", "_git/repo"),
        ("", "proj/_git/repo"),
        ("o", "\ud800/_git/r"),
    ]
    for host in hosts:
        for owner, name in pairs:
            yield host, owner, name


def _urlparse(url: str) -> list[Any] | str:
    try:
        p = urlparse(url)
        hostname = p.hostname
    except ValueError as exc:
        msg = str(exc)
        for prefix, kind in (
            ("Invalid IPv6 URL", "ipv6"),
            ("IPvFuture", "future"),
            ("An IPv4 address", "ipv4"),
            ("netloc ", "nfkc"),
        ):
            if msg.startswith(prefix):
                return kind
        if msg.endswith("does not appear to be an IPv4 or IPv6 address"):
            return "address"
        raise
    parts = [p.scheme, p.netloc, p.path, p.params, p.query, p.fragment]
    return [*(_s(x) for x in parts), None if hostname is None else _s(hostname)]


_SCHEME_PREFIXES = (
    "http://",
    "https://",
    "git://",
    "ssh://",
    "git+ssh://",
    "ssh+git://",
)


def _gate(texts: list[str]) -> str:
    if any("[" in t or "]" in t for t in texts):
        return "python"
    if any(not t.isascii() for t in texts):
        return "unicode"
    return ""


def _rows() -> Iterator[tuple[str, list[Any]]]:
    for url in dict.fromkeys(_urls()):
        yield _gate([url]), ["p", _s(url), _t(origin_remote._parse_remote(url))]
        stripped = url.strip()
        if stripped.startswith(_SCHEME_PREFIXES):
            yield _gate([stripped]), ["u", _s(stripped), _urlparse(stripped)]
    for triple in _triples():
        gate = _gate(list(triple))
        yield gate, ["c", _t(triple), _t(origin_remote._canonicalize(*triple))]
        yield gate, ["z", _t(triple), _t(origin_remote._canonicalize_azure(*triple))]


def _file(which: str) -> bytes:
    rows = [row for gate, row in _rows() if gate == which]
    counts = {k: sum(1 for r in rows if r[0] == k) for k in ("p", "u", "c", "z")}
    name = "origin_remote" + (f"_{which}" if which else "")
    return jsonl([{"fixture": name, **counts}, *rows])


def generate() -> dict[str, bytes]:
    return {"origin_remote.jsonl": _file("")}


def generate_unicode() -> dict[str, bytes]:
    return {"origin_remote_unicode.jsonl": _file("unicode")}


def generate_python() -> dict[str, bytes]:
    return {"origin_remote_python.jsonl": _file("python")}
