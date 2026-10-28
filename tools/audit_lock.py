#!/usr/bin/env python3
"""Audit uv.lock against the OSV vulnerability database.

    python3 tools/audit_lock.py [--lock uv.lock] [--scope all|runtime]
        [--ignore-file tools/audit-ignore.toml] [--format text|json]
        [--offline FIXTURE]

Every PyPI package the lock pins, runtime and dev alike (``--scope
runtime`` narrows it to what the project runs with), is sent as one
``(name, version)`` query to OSV's ``/v1/querybatch``; each advisory that
comes back is then read from ``/v1/vulns/{id}`` for its summary, aliases,
severity and fixed versions, and the records OSV keeps of one vulnerability
(a GHSA and a PYSEC naming each other) are reported as one. The project
itself and a package from git, a path or another index are listed as not
audited. ``.github/workflows/audit.yml`` runs it daily,
on pushes and on pull requests that touch the lock. It does not gate a
release: a new advisory against a locked version is news about the world,
not about the commit being tagged.

An advisory can be ignored, never silently: ``tools/audit-ignore.toml``
holds one ``[[ignore]]`` table per accepted advisory, each with the
advisory's ``id`` (or any of its aliases, a CVE id for instance), a
``reason`` and an ``expires`` date. The entry holds through that date and no
later; an expired entry fails the audit whether or not its advisory is still
reported, so an acceptance is re-decided rather than forgotten.

Exit status: 0 when no advisory without a live ignore entry affects a
locked version; 1 when one does, or when an ignore entry has expired; 2 when
the audit could not run (an unreadable lock or ignore file, OSV unreachable
after retries). A failed audit never reads as a clean one.

``--offline FIXTURE`` answers from a JSON file instead of the network, for
the tests (``tests/test_audit_lock.py``): ``{"results": {"<name>@<version>":
<one querybatch result>}, "vulns": {"<id>": <one OSV vulnerability>}}``, each
value in OSV's own response shape. Stdlib only, so the workflow runs it on
the runner's ``python3`` with nothing installed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import _uvlock

OSV_API = "https://api.osv.dev"
#: OSV's documented ceiling on queries per querybatch request.
BATCH_LIMIT = 1000
USER_AGENT = "bettermemory-audit-lock (+https://github.com/0Mattias/bettermemory)"

OPEN = "open"
IGNORED = "ignored"
EXPIRED = "expired"


class AuditError(Exception):
    """The audit could not run: exit status 2."""


class Osv(Protocol):
    """The two OSV calls the audit makes, so the tests can answer them."""

    def querybatch(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def vuln(self, vuln_id: str) -> dict[str, Any]: ...


Opener = Callable[..., Any]


class OsvClient:
    """OSV over HTTPS, retried the way publish-mcp.yml retries PyPI: a few
    attempts with a growing wait, then a loud failure."""

    def __init__(
        self,
        base: str = OSV_API,
        *,
        timeout: float = 30.0,
        attempts: int = 3,
        backoff: float = 5.0,
        opener: Opener = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self._open = opener
        self._sleep = sleep

    def _request(self, path: str, payload: object | None = None) -> Any:
        url = f"{self.base}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="GET" if data is None else "POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        failure = "no attempt made"
        for attempt in range(1, self.attempts + 1):
            try:
                with self._open(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                failure = f"HTTP {exc.code}"
                # A client error will not change on a retry; a rate limit or
                # a server error might.
                if exc.code != 429 and exc.code < 500:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                failure = str(getattr(exc, "reason", None) or exc)
            if attempt < self.attempts:
                self._sleep(self.backoff * attempt)
        raise AuditError(f"OSV {url}: {failure}")

    def querybatch(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        answer = self._request("/v1/querybatch", {"queries": queries})
        results = answer.get("results") if isinstance(answer, Mapping) else None
        if not isinstance(results, list) or len(results) != len(queries):
            raise AuditError(
                "OSV querybatch: the answer does not hold one result per query"
            )
        return results

    def vuln(self, vuln_id: str) -> dict[str, Any]:
        answer = self._request(f"/v1/vulns/{urllib.parse.quote(vuln_id, safe='')}")
        if not isinstance(answer, dict):
            raise AuditError(f"OSV vulns/{vuln_id}: the answer is not an object")
        return answer


class OsvFixture:
    """OSV answered from a recorded JSON file (``--offline``)."""

    def __init__(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuditError(f"{path}: {exc}") from exc
        # A fixture of the wrong shape is an audit that cannot run (exit 2),
        # not a traceback, whose exit status 1 would read as a finding.
        if not isinstance(data, Mapping):
            raise AuditError(f"{path}: not a JSON object")
        results = data.get("results", {})
        vulns = data.get("vulns", {})
        if not isinstance(results, Mapping) or not isinstance(vulns, Mapping):
            raise AuditError(f"{path}: `results` and `vulns` must be objects")
        self._results: Mapping[str, Any] = results
        self._vulns: Mapping[str, Any] = vulns
        self.path = path

    def querybatch(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for query in queries:
            name = _uvlock.normalize(query["package"]["name"])
            out.append(dict(self._results.get(f"{name}@{query['version']}", {})))
        return out

    def vuln(self, vuln_id: str) -> dict[str, Any]:
        if vuln_id not in self._vulns:
            raise AuditError(f"{self.path}: no recorded vulnerability {vuln_id}")
        return dict(self._vulns[vuln_id])


@dataclass(frozen=True)
class Locked:
    """A package the audit asks about."""

    name: str
    version: str
    set: str


@dataclass(frozen=True)
class IgnoreEntry:
    """One ``[[ignore]]`` table of the ignore file."""

    id: str
    reason: str
    expires: dt.date
    #: Narrows the entry to one package; None matches the id on any.
    package: str | None = None

    def expired(self, today: dt.date) -> bool:
        return today > self.expires

    def matches(self, advisory: Advisory, package: str) -> bool:
        if self.package is not None and _uvlock.normalize(self.package) != package:
            return False
        wanted = self.id.upper()
        return wanted == advisory.id.upper() or wanted in {
            a.upper() for a in advisory.aliases
        }

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reason": self.reason,
            "expires": self.expires.isoformat(),
            "package": self.package,
        }


@dataclass(frozen=True)
class Advisory:
    """What the audit reports of one OSV vulnerability for one package."""

    id: str
    aliases: tuple[str, ...]
    summary: str
    severity: str | None
    fixed: tuple[str, ...]


@dataclass
class Finding:
    package: Locked
    advisory: Advisory
    status: str
    ignore: IgnoreEntry | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.advisory.id,
            "aliases": list(self.advisory.aliases),
            "package": self.package.name,
            "version": self.package.version,
            "set": self.package.set,
            "summary": self.advisory.summary,
            "severity": self.advisory.severity,
            "fixed": list(self.advisory.fixed),
            "url": f"https://osv.dev/vulnerability/{self.advisory.id}",
            "status": self.status,
            "ignore": None if self.ignore is None else self.ignore.as_json(),
        }


@dataclass
class Report:
    lock: str
    scope: str
    source: str
    audited: list[Locked]
    skipped: list[str]
    findings: list[Finding] = field(default_factory=list)
    expired_entries: list[IgnoreEntry] = field(default_factory=list)
    unused_entries: list[IgnoreEntry] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.expired_entries) or any(
            f.status != IGNORED for f in self.findings
        )

    def as_json(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for package in self.audited:
            counts[package.set] = counts.get(package.set, 0) + 1
        return {
            "lock": self.lock,
            "scope": self.scope,
            "source": self.source,
            "packages": counts,
            "skipped": self.skipped,
            "findings": [f.as_json() for f in self.findings],
            "ignore_entries": {
                "expired": [e.as_json() for e in self.expired_entries],
                "unused": [e.as_json() for e in self.unused_entries],
            },
            "withdrawn": self.withdrawn,
            "status": "fail" if self.failed else "ok",
        }


def load_ignores(path: Path) -> list[IgnoreEntry]:
    """The ignore file's entries. A missing file is no entries; an entry
    without an id, a reason or a valid ``expires`` date is an AuditError, so
    a half-written acceptance cannot pass for a whole one."""
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuditError(f"{path}: {exc}") from exc
    tables = data.get("ignore", [])
    if not isinstance(tables, list):
        raise AuditError(f"{path}: `ignore` must be an array of tables ([[ignore]])")
    entries: list[IgnoreEntry] = []
    seen: set[str] = set()
    for index, table in enumerate(tables, start=1):
        where = f"{path}: [[ignore]] #{index}"
        if not isinstance(table, Mapping):
            raise AuditError(f"{where} is not a table")
        vuln_id = table.get("id")
        reason = table.get("reason")
        expires = table.get("expires")
        package = table.get("package")
        if not isinstance(vuln_id, str) or not vuln_id.strip():
            raise AuditError(f"{where} has no id")
        if not isinstance(reason, str) or not reason.strip():
            raise AuditError(f"{where} ({vuln_id}) has no reason")
        if isinstance(expires, str):
            try:
                expires = dt.date.fromisoformat(expires)
            except ValueError as exc:
                raise AuditError(
                    f"{where} ({vuln_id}): expires {expires!r}: {exc}"
                ) from exc
        # A TOML datetime is a dt.datetime, itself a dt.date subclass: refuse
        # it, since "expires at 14:00 in which zone" is not a question an
        # acceptance should leave open.
        if not isinstance(expires, dt.date) or isinstance(expires, dt.datetime):
            raise AuditError(f"{where} ({vuln_id}) has no expires date (YYYY-MM-DD)")
        if package is not None and not isinstance(package, str):
            raise AuditError(f"{where} ({vuln_id}): package must be a string")
        # Compared the way `matches` compares them, so two spellings of one
        # package (or a stray space around an id) cannot list an id twice.
        narrowed = _uvlock.normalize(package) if package else ""
        key = f"{vuln_id.strip().upper()}|{narrowed}"
        if key in seen:
            raise AuditError(f"{where}: {vuln_id} is listed twice")
        seen.add(key)
        entries.append(
            IgnoreEntry(
                id=vuln_id.strip(),
                reason=reason.strip(),
                expires=expires,
                package=package,
            )
        )
    return entries


def _severity(doc: Mapping[str, Any]) -> str | None:
    """GitHub's reviewed rating when the advisory carries one (LOW,
    MODERATE, HIGH, CRITICAL), else the first CVSS vector OSV lists."""
    specific = doc.get("database_specific")
    if isinstance(specific, Mapping) and isinstance(specific.get("severity"), str):
        return str(specific["severity"])
    for entry in doc.get("severity") or ():
        if isinstance(entry, Mapping) and isinstance(entry.get("score"), str):
            return str(entry["score"])
    return None


def _fixed(doc: Mapping[str, Any], name: str) -> tuple[str, ...]:
    """The versions OSV says fix the advisory for `name` on PyPI, in the
    order the advisory lists them."""
    found: list[str] = []
    for affected in doc.get("affected") or ():
        package = affected.get("package") or {}
        if package.get("ecosystem") != "PyPI":
            continue
        if _uvlock.normalize(str(package.get("name", ""))) != name:
            continue
        for span in affected.get("ranges") or ():
            for event in span.get("events") or ():
                version = event.get("fixed")
                if isinstance(version, str) and version not in found:
                    found.append(version)
    return tuple(found)


def advisory(doc: Mapping[str, Any], name: str) -> Advisory:
    """The parts of an OSV vulnerability the report shows, for `name`."""
    summary = doc.get("summary") or doc.get("details") or ""
    summary = " ".join(str(summary).split())
    if len(summary) > 200:
        summary = summary[:197] + "..."
    return Advisory(
        id=str(doc["id"]),
        aliases=tuple(str(a) for a in doc.get("aliases") or ()),
        summary=summary,
        severity=_severity(doc),
        fixed=_fixed(doc, name),
    )


#: Which record of a vulnerability leads when several describe it: GitHub's
#: (its severity is a reviewed rating), then PyPA's, then any other.
_PREFERRED = ("GHSA-", "PYSEC-")


def _preference(vuln_id: str) -> tuple[int, str]:
    for rank, prefix in enumerate(_PREFERRED):
        if vuln_id.startswith(prefix):
            return (rank, vuln_id)
    return (len(_PREFERRED), vuln_id)


def fold(docs: Iterable[Mapping[str, Any]], name: str) -> list[Advisory]:
    """One advisory per vulnerability. OSV answers a query with every record
    that names the version, and one vulnerability usually has several:
    GitHub's GHSA and PyPA's PYSEC, each listing the other among its
    aliases. Records whose ids and aliases overlap are one finding, led by
    the preferred record, carrying every other id as an alias and the union
    of their fixed versions, so a report of nine vulnerabilities reads nine
    and not eighteen.

    The overlap is followed through: a record met later can name ids from
    two groups that do not name each other (a GHSA listing CVE-1, a PYSEC
    listing CVE-2, a third record listing both), and then the two groups
    are one vulnerability and merge. Left apart, the second would stay open
    after an ignore entry named the first."""
    groups: list[tuple[set[str], list[Mapping[str, Any]]]] = []
    for doc in sorted(docs, key=lambda d: _preference(str(d["id"]))):
        names = {
            str(doc["id"]).upper(),
            *(str(a).upper() for a in doc.get("aliases") or ()),
        }
        joined = [group for group in groups if group[0] & names]
        if not joined:
            groups.append((names, [doc]))
            continue
        known, members = joined[0]
        known |= names
        members.append(doc)
        for other_known, other_members in joined[1:]:
            known |= other_known
            members.extend(other_members)
        groups = [g for g in groups if not any(g is other for other in joined[1:])]
    out: list[Advisory] = []
    for _known, members in groups:
        # Merging can interleave the groups' records; the lead and the order
        # of fixed versions follow the preference order either way.
        members.sort(key=lambda d: _preference(str(d["id"])))
        lead = advisory(members[0], name)
        aliases = {
            str(a) for m in members for a in (m["id"], *(m.get("aliases") or ()))
        }
        fixed = list(lead.fixed)
        for member in members[1:]:
            fixed.extend(v for v in _fixed(member, name) if v not in fixed)
        severity = lead.severity
        for member in members[1:]:
            severity = severity or _severity(member)
        out.append(
            Advisory(
                id=lead.id,
                aliases=tuple(sorted(aliases - {lead.id})),
                summary=lead.summary,
                severity=severity,
                fixed=tuple(fixed),
            )
        )
    return out


def query(osv: Osv, packages: Sequence[Locked]) -> dict[Locked, list[str]]:
    """The OSV ids reported against each package, in batches OSV accepts,
    following any page token until each package's list is complete."""
    found: dict[Locked, list[str]] = {p: [] for p in packages}
    pending: list[tuple[Locked, str | None]] = [(p, None) for p in packages]
    while pending:
        batch, pending = pending[:BATCH_LIMIT], pending[BATCH_LIMIT:]
        queries: list[dict[str, Any]] = []
        for package, token in batch:
            q: dict[str, Any] = {
                "package": {"name": package.name, "ecosystem": "PyPI"},
                "version": package.version,
            }
            if token:
                q["page_token"] = token
            queries.append(q)
        for (package, _token), result in zip(batch, osv.querybatch(queries)):
            for vuln in result.get("vulns") or ():
                vuln_id = vuln.get("id")
                if isinstance(vuln_id, str) and vuln_id not in found[package]:
                    found[package].append(vuln_id)
            follow = result.get("next_page_token")
            if isinstance(follow, str) and follow:
                pending.append((package, follow))
    return found


def locked_packages(
    lock: _uvlock.Lock, root: _uvlock.Package, scope: str
) -> tuple[list[Locked], list[str]]:
    """The PyPI packages to audit, and a line for each package of the scope
    the audit cannot ask about: a git, path or URL source has no registry
    version, and a package from another index is not the PyPI project of
    the same name, whose advisories OSV would answer with."""
    sets = lock.sets(root)
    audited: list[Locked] = []
    skipped: list[str] = []
    for package in lock.packages:
        if package.key == root.key:
            continue
        label = sets[package.key]
        if scope == _uvlock.RUNTIME and label != _uvlock.RUNTIME:
            continue
        if not package.pypi:
            kind, where = package.source
            skipped.append(f"{package.name} {package.version} ({kind} {where})".strip())
            continue
        audited.append(Locked(name=package.name, version=package.version, set=label))
    return audited, skipped


def audit(
    lock: _uvlock.Lock,
    root: _uvlock.Package,
    osv: Osv,
    ignores: Iterable[IgnoreEntry],
    *,
    scope: str = "all",
    today: dt.date,
    lock_label: str = "uv.lock",
    source: str = OSV_API,
) -> Report:
    entries = list(ignores)
    audited, skipped = locked_packages(lock, root, scope)
    report = Report(
        lock=lock_label, scope=scope, source=source, audited=audited, skipped=skipped
    )
    used: set[int] = set()
    cache: dict[str, dict[str, Any]] = {}
    for package, ids in query(osv, audited).items():
        docs: list[dict[str, Any]] = []
        for vuln_id in ids:
            if vuln_id not in cache:
                doc = osv.vuln(vuln_id)
                if not isinstance(doc.get("id"), str):
                    doc = {**doc, "id": vuln_id}
                cache[vuln_id] = doc
            if cache[vuln_id].get("withdrawn"):
                report.withdrawn.append(f"{vuln_id} ({package.name} {package.version})")
                continue
            docs.append(cache[vuln_id])
        for found in fold(docs, package.name):
            match = next(
                (i for i, e in enumerate(entries) if e.matches(found, package.name)),
                None,
            )
            if match is None:
                report.findings.append(Finding(package, found, OPEN))
                continue
            used.add(match)
            entry = entries[match]
            status = EXPIRED if entry.expired(today) else IGNORED
            report.findings.append(Finding(package, found, status, entry))
    for index, entry in enumerate(entries):
        if entry.expired(today):
            report.expired_entries.append(entry)
        elif index not in used:
            report.unused_entries.append(entry)
    report.findings.sort(
        key=lambda f: (
            f.status == IGNORED,
            f.package.set != _uvlock.RUNTIME,
            f.package.name,
            f.advisory.id,
        )
    )
    return report


def render_text(report: Report) -> str:
    counts = report.as_json()["packages"]
    shown = ", ".join(
        f"{n} {label}" for label, n in sorted(counts.items(), reverse=True)
    )
    lines = [
        f"audit_lock: {len(report.audited)} packages from {report.lock} "
        f"({shown or 'none'}), scope {report.scope}, source {report.source}"
    ]
    for line in report.skipped:
        lines.append(f"  not audited (not a PyPI package): {line}")
    for finding in report.findings:
        a = finding.advisory
        tag = {
            OPEN: "VULNERABLE",
            IGNORED: "ignored",
            EXPIRED: "VULNERABLE (ignore expired)",
        }[finding.status]
        lines.append(
            f"{tag}: {a.id} {finding.package.name} {finding.package.version} "
            f"[{finding.package.set}{', ' + a.severity if a.severity else ''}]"
        )
        if a.summary:
            lines.append(f"  {a.summary}")
        if a.aliases:
            lines.append(f"  aliases: {', '.join(a.aliases)}")
        lines.append(
            f"  fixed in: {', '.join(a.fixed) if a.fixed else 'no fixed version listed'}"
        )
        lines.append(f"  https://osv.dev/vulnerability/{a.id}")
        if finding.ignore is not None:
            lines.append(
                f"  ignore entry: {finding.ignore.id}, expires {finding.ignore.expires}: "
                f"{finding.ignore.reason}"
            )
    for entry in report.expired_entries:
        lines.append(
            f"EXPIRED ignore entry: {entry.id} (expired {entry.expires}): re-decide it, "
            f"renew its date with a fresh reason, or remove it"
        )
    for entry in report.unused_entries:
        lines.append(
            f"note: ignore entry {entry.id} matches no current advisory; remove it "
            f"(it expires {entry.expires})"
        )
    for line in report.withdrawn:
        lines.append(f"note: withdrawn advisory not reported: {line}")
    open_count = sum(1 for f in report.findings if f.status != IGNORED)
    ignored = sum(1 for f in report.findings if f.status == IGNORED)
    lines.append(
        f"result: {'FAIL' if report.failed else 'ok'}: {open_count} open advisories, "
        f"{ignored} ignored, {len(report.expired_entries)} expired ignore entries"
    )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None, *, today: dt.date | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit uv.lock against the OSV vulnerability database.",
        epilog="Exit status: 0 clean, 1 an open advisory or an expired ignore entry, "
        "2 the audit could not run.",
    )
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="the pyproject.toml naming the project (default: beside the lock)",
    )
    parser.add_argument("--scope", choices=("all", _uvlock.RUNTIME), default="all")
    parser.add_argument(
        "--ignore-file",
        type=Path,
        default=Path(__file__).with_name("audit-ignore.toml"),
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--offline",
        type=Path,
        default=None,
        metavar="FIXTURE",
        help="answer from a recorded JSON file instead of OSV (tests)",
    )
    args = parser.parse_args(argv)
    if today is None:
        today = dt.datetime.now(dt.timezone.utc).date()
    try:
        lock = _uvlock.load(args.lock)
        pyproject = args.project or args.lock.with_name("pyproject.toml")
        root = lock.root(_uvlock.project(pyproject)[0] if pyproject.exists() else None)
        ignores = load_ignores(args.ignore_file)
        osv: Osv
        if args.offline is not None:
            osv = OsvFixture(args.offline)
            source = f"offline fixture {args.offline}"
        else:
            osv = OsvClient()
            source = OSV_API
        report = audit(
            lock,
            root,
            osv,
            ignores,
            scope=args.scope,
            today=today,
            lock_label=str(args.lock),
            source=source,
        )
    except (AuditError, _uvlock.LockError) as exc:
        print(f"audit_lock: cannot audit: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        sys.stdout.write(json.dumps(report.as_json(), indent=2) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
