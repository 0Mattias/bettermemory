"""Remote URL parsing for `origin`: what "same project" compares.

`_parse_remote` reads a remote URL, scp-like SSH or URL-form, into the
``(host, owner, name)`` triple `origin.repos_match` compares, and
`_canonicalize` folds fixed vendor shapes onto one triple: the
first-party SSH-over-443 alias hosts, Azure DevOps's clone forms, and
the Bitbucket Server and Gerrit routing prefixes. The comment block
below lists the forms read and why arbitrary mount prefixes are not.

This module lives apart from `origin.py` to keep each module under the
package's 2,000-line bound (ENG-96). It imports only the standard
library; `origin` imports it, never the reverse, and re-exports
`_parse_remote`, which tests read through `origin`.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Remote URL parsing
# ---------------------------------------------------------------------------
#
# We accept the forms `git remote get-url origin` typically emits:
#   [git@]github.com:owner/name.git         (scp-like SSH; user optional)
#   https://github.com/owner/name.git       (HTTPS)
#   ssh:// git:// git+ssh:// ssh+git://     (URL-form transports)
# plus single-segment (ownerless) paths — gitolite, Gerrit SSH, cgit-style
# HTTPS at the domain root. A small set of FIXED vendor shapes is then
# canonicalized: Azure DevOps's protocol-asymmetric clone URLs, Bitbucket
# Server's '/scm/' and Gerrit's authenticated '/a/' routing prefixes (each
# stripped only on hosts whose name carries the vendor's), and
# the first-party SSH-over-443 alias hosts. Arbitrary mount prefixes
# (GitLab installed under a relative URL root, smart-HTTP behind Apache's
# '/git/') are DELIBERATELY not stripped: an arbitrary prefix is
# syntactically indistinguishable from an owner or subgroup, and stripping
# it would merge distinct projects — the cross-project leakage the origin
# match fails closed against. Those remotes mismatch their SSH form (a false
# negative, the tolerated direction) rather than risk a false positive.

# Host charset excludes '/' (so slash-before-colon local paths like
# './local/path:odd' never parse as remotes) and requires >=2 chars (so
# Windows drive paths like 'C:/Users/...' fall through to the raw-equality
# fallback). The `(?!//)` lookahead keeps every scheme-prefixed URL —
# including unknown schemes — out of the scp branch.
_SSH_REMOTE_RE = re.compile(
    r"^(?:[a-zA-Z0-9_.+-]+@)?([^/:@]{2,}):(?!//)/?(?:([^/]+)/)?(.+?)(?:\.git)?/?$"
)

# Fixed, first-party alternate hostnames serving the same repositories as
# the canonical host — GitHub's and GitLab's documented SSH-over-443
# fallbacks for port-22-blocked networks. Deliberately NOT user-defined
# ssh-config aliases (github.com-work etc.), which would require reading
# the user's SSH config.
_HOST_ALIASES = {
    "ssh.github.com": "github.com",
    "altssh.gitlab.com": "gitlab.com",
}

# Fixed vendor HTTP(S) routing prefixes that precede the real owner/name:
# 'scm' (Bitbucket Server/Data Center clone URLs) and 'a' (Gerrit's
# authenticated-HTTP prefix), each paired with a host substring that must
# appear in the URL's hostname for the strip to fire. Stripping on EVERY
# host violated the never-widen invariant on nested-namespace hosts:
# GitLab subgroups make 'https://gitlab.com/scm/team/proj' a real
# top-level group named 'scm', and the unconditional strip merged it
# with the unrelated 'https://gitlab.com/team/proj' (while the same
# repo's unstripped SSH form failed to match its own HTTPS form).
# Bitbucket Server / Gerrit instances whose hostname doesn't carry the
# vendor name fall back to the tolerated false-negative direction —
# their http(s) form mismatches their ssh form — exactly like arbitrary
# mount prefixes. The strip is also gated on the remainder still
# containing '/', so a real single-char owner (github.com/a/repo) keeps
# parsing as owner='a'. Frozen by design — see the comment block above
# for why arbitrary mount prefixes stay unhandled.
_VENDOR_ROUTE_PREFIXES: dict[str, str] = {"scm": "bitbucket", "a": "gerrit"}


def _parse_remote(url: str) -> tuple[str, str, str] | None:
    """Parse a remote URL into (host, owner, name). Returns None when the
    URL can't be parsed — caller falls back to raw string comparison.

    `owner` is "" for single-segment (ownerless) paths. The empty-owner
    sentinel keeps that relaxation collision-free: a single-segment remote
    can only ever match another single-segment remote on the same host,
    because a two-segment URL always parses with a non-empty owner.
    """
    url = url.strip()
    if not url:
        return None

    m = _SSH_REMOTE_RE.match(url)
    if m:
        host, owner, name = m.group(1), m.group(2) or "", m.group(3)
        # `name` may still carry a trailing `.git` if the regex's
        # non-greedy capture matched up to a slash before it.
        name = name.removesuffix(".git").rstrip("/")
        if not name:
            return None
        return _canonicalize(host, owner, name)

    if url.startswith(
        ("http://", "https://", "git://", "ssh://", "git+ssh://", "ssh+git://")
    ):
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        host = parsed.hostname or ""
        path = parsed.path.strip("/")
        if not host or not path:
            return None
        path = path.removesuffix(".git").rstrip("/")
        if not path:
            return None
        owner, _, name = path.partition("/")
        if not name:
            # Single path segment — an ownerless root-mounted repo.
            owner, name = "", owner
        elif (
            parsed.scheme in ("http", "https")
            and "/" in name
            and (hint := _VENDOR_ROUTE_PREFIXES.get(owner.lower())) is not None
            and hint in host.lower()
        ):
            # Fixed vendor routing prefix, not an owner — re-split the
            # remainder. The contains-'/' guard keeps github.com/a/repo
            # parsing as owner='a'; the host gate keeps a real top-level
            # group named 'scm'/'a' on a nested-namespace host (GitLab
            # subgroups) from merging with the repo at the stripped path.
            owner, name = name.split("/", 1)
        return _canonicalize(host, owner, name)

    return None


def _canonicalize(host: str, owner: str, name: str) -> tuple[str, str, str]:
    """Vendor normalization applied to every parsed triple.

    Maps fixed first-party alias hosts onto the canonical host and
    collapses Azure DevOps's protocol-asymmetric clone shapes onto one
    triple. Anything that doesn't exactly fit a known vendor shape passes
    through unchanged — normalization here may only ever MERGE official
    spellings of the same repository, never widen beyond them.
    """
    host = _HOST_ALIASES.get(host.lower(), host)
    azure = _canonicalize_azure(host, owner, name)
    if azure is not None:
        return azure
    return host, owner, name


def _canonicalize_azure(
    host: str, owner: str, name: str
) -> tuple[str, str, str] | None:
    """Collapse the official Azure DevOps clone forms onto
    ('dev.azure.com', org, '{project}/{repo}'). Returns None for anything
    that doesn't exactly fit an official shape — the caller keeps the
    generic parse, so non-conforming URLs degrade to today's behavior
    instead of widening matching.

    Official forms (protocol-asymmetric, hence never matching under the
    generic owner/name split):
      SSH     git@ssh.dev.azure.com:v3/{org}/{project}/{repo}
      HTTPS   https://{org}@dev.azure.com/{org}/{project}/_git/{repo}
      legacy  https://{org}.visualstudio.com/[DefaultCollection/]{project}/_git/{repo}
      legacy  git@vs-ssh.visualstudio.com:v3/{org}/{project}/{repo}
    """
    h = host.lower()
    if h in ("ssh.dev.azure.com", "vs-ssh.visualstudio.com"):
        # Generic parse yields owner='v3', name='{org}/{project}/{repo}'.
        if owner.lower() == "v3":
            segs = name.split("/")
            if len(segs) == 3 and all(segs):
                org, project, repo = segs
                return "dev.azure.com", org, f"{project}/{repo}"
        return None
    if h == "dev.azure.com":
        # Generic parse yields owner='{org}', name='{project}/_git/{repo}'.
        segs = name.split("/")
        if len(segs) == 3 and segs[1] == "_git" and owner and segs[0] and segs[2]:
            return "dev.azure.com", owner, f"{segs[0]}/{segs[2]}"
        return None
    if h.endswith(".visualstudio.com"):
        # The org is the subdomain; the path may carry a leading
        # 'DefaultCollection' segment on older clones.
        org = h.removesuffix(".visualstudio.com")
        if not org or "." in org:
            return None
        segs = [owner, *name.split("/")] if owner else name.split("/")
        if segs and segs[0] == "DefaultCollection":
            segs = segs[1:]
        if len(segs) == 3 and segs[1] == "_git" and segs[0] and segs[2]:
            return "dev.azure.com", org, f"{segs[0]}/{segs[2]}"
        return None
    return None
