"""One claimed file is one claim, whichever spelling of it a read meets.

The reads keyed a claim's identity (the seen, absent and verified sets) on
a lexical form, while the containment test that admits an absolute
attestation resolves both sides. So with a root spelled through a link
(macOS's ``/tmp`` for ``/private/tmp``, ``/var`` for ``/private/var``) and
an absence attested under the resolved spelling, the attestation pass read
the file ``expected_absent`` and the citation pass read it again under the
root's spelling and reported it ``claim_anchored_missing``, which convicts
the record (ENG-162). On Windows a short 8.3 name or another case of a
component is the same mismatch. Every set now keys on
`verify_candidates._claim_key`: a non-strict ``realpath`` in this
platform's case.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from bettermemory.verify import ClaimAnchor, PathDriftReport, detect_path_drift
from bettermemory import verify_anchoring, verify_candidates

_BODY = "The old shim `src/gone.py` was removed; the loader is `src/a.py`."


def _tree(base: Path) -> Path:
    proj = base / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.py").write_text("x = 1\n")
    return proj


def _aliases(tmp_path: Path) -> tuple[Path, list[Path]]:
    """The project's resolved spelling and the other spellings of it this
    platform can make: through a symbolic link to its parent, and through
    macOS's ``/var`` where the temporary directory is under
    ``/private/var``."""
    real = _tree(tmp_path / "real")
    resolved = Path(os.path.realpath(real))
    out: list[Path] = []
    link = tmp_path / "alias"
    try:
        link.symlink_to(tmp_path / "real", target_is_directory=True)
        out.append(link / "proj")
    except OSError:
        pass
    text = str(resolved)
    if text.startswith("/private/var/") and os.path.realpath("/var") == "/private/var":
        out.append(Path(text[len("/private") :]))
    for spelled in out:
        assert str(spelled) != str(resolved) and os.path.samefile(spelled, resolved)
    return resolved, out


def _read(
    reader: str,
    root: Path,
    *,
    verified_paths: tuple[str, ...] = (),
    absent_paths: tuple[str, ...] = (),
) -> PathDriftReport:
    if reader == "anchor":
        return detect_path_drift(
            _BODY,
            verified_paths=verified_paths,
            absent_paths=absent_paths,
            anchor=ClaimAnchor(root=str(root), rule="home_checkout"),
        )
    return detect_path_drift(
        _BODY,
        verified_paths=verified_paths,
        absent_paths=absent_paths,
        worktree_root=str(root),
    )


def _ends(paths: tuple[str, ...], tail: str) -> list[str]:
    return [p for p in paths if p.replace(os.sep, "/").endswith(tail)]


def _pairs(tmp_path: Path) -> list[tuple[Path, Path]]:
    resolved, aliases = _aliases(tmp_path)
    if not aliases:
        pytest.skip("no other spelling of a directory can be made here")
    return [
        pair for alias in aliases for pair in ((alias, resolved), (resolved, alias))
    ]


@pytest.mark.parametrize("reader", ["worktree_root", "anchor"])
def test_an_absence_attested_under_another_spelling_reads_once(
    tmp_path: Path, reader: str
) -> None:
    for root, spelled in _pairs(tmp_path):
        absent = (str(spelled / "src" / "gone.py"),)
        report = _read(reader, root, absent_paths=absent)
        assert len(_ends(report.expected_absent, "src/gone.py")) == 1, (root, spelled)
        assert not report.claim_anchored_missing, (root, spelled)
        assert not report.missing, (root, spelled)
        assert len(_ends(report.checked, "src/gone.py")) == 1, (root, spelled)


@pytest.mark.parametrize("reader", ["worktree_root", "anchor"])
def test_a_file_verified_under_another_spelling_is_checked_once(
    tmp_path: Path, reader: str
) -> None:
    for root, spelled in _pairs(tmp_path):
        verified = (str(spelled / "src" / "a.py"),)
        report = _read(reader, root, verified_paths=verified)
        assert len(_ends(report.checked, "src/a.py")) == 1, (root, spelled)
        assert len(_ends(report.verified, "src/a.py")) == 1, (root, spelled)


def test_the_key_resolves_links_and_keeps_an_unresolvable_spelling(
    tmp_path: Path,
) -> None:
    resolved, aliases = _aliases(tmp_path)
    for alias in aliases:
        assert verify_candidates._claim_key(
            str(alias / "src" / "gone.py")
        ) == verify_candidates._claim_key(str(resolved / "src" / "gone.py"))
    if os.name != "nt":
        drive = "C:\\Users\\me\\proj\\src\\a.py"
        assert verify_candidates._claim_key(drive) == drive


@pytest.mark.skipif(os.name != "nt", reason="the case of a name folds on Windows")
@pytest.mark.parametrize("reader", ["worktree_root", "anchor"])
def test_a_windows_case_variant_reads_once(tmp_path: Path, reader: str) -> None:
    root = _tree(tmp_path)
    spelled = tmp_path / "PROJ"
    assert os.path.samefile(spelled, root), "premise: NTFS folds case"
    report = _read(reader, root, absent_paths=(str(spelled / "src" / "gone.py"),))
    assert len(_ends(report.expected_absent, "src/gone.py")) == 1
    assert not report.claim_anchored_missing
    assert not report.missing
    assert verify_candidates._claim_key(
        str(spelled / "src" / "a.py")
    ) == verify_candidates._claim_key(str(root / "src" / "a.py"))


def test_the_read_keys_resolve_as_realpath_does(tmp_path: Path) -> None:
    """`_ClaimKeys` resolves each directory once per read; what it answers
    is what `_claim_key` (a non-strict ``realpath``) answers, through
    absolute, relative, dangling and looping links, ``.`` and ``..``,
    doubled and trailing separators and missing components."""
    base = Path(os.path.realpath(tmp_path)) / "base"
    (base / "real" / "sub").mkdir(parents=True)
    (base / "real" / "sub" / "f.py").write_text("x = 1\n")
    try:
        (base / "abs_link").symlink_to(base / "real", target_is_directory=True)
        (base / "rel_link").symlink_to("real", target_is_directory=True)
        (base / "dangling").symlink_to(base / "nowhere")
        (base / "loop_a").symlink_to(base / "loop_b")
        (base / "loop_b").symlink_to(base / "loop_a")
        (base / "real" / "up").symlink_to("..", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links cannot be made here")
    paths = [
        str(base / "real" / "sub" / "f.py"),
        str(base / "abs_link" / "sub" / "f.py"),
        str(base / "rel_link" / "sub" / "missing" / "x.py"),
        str(base / "dangling" / "x.py"),
        str(base / "loop_a" / "x.py"),
        f"{base}/real/./sub/../sub/f.py",
        f"{base}/abs_link/../real/sub",
        f"{base}/real/up/real/sub/f.py",
        f"{base}/real/sub/",
        f"{base}//real//sub",
        "/",
        "src/relative.py",
        "",
    ]
    keys = verify_candidates._ClaimKeys()
    for path in paths:
        assert keys(path) == verify_candidates._claim_key(path), path
    for path in reversed(paths):
        assert keys(path) == verify_candidates._claim_key(path), path


#: Twelve directories between the temporary directory and the project.
_DEEP = tuple(f"d{i}" for i in range(12))


@pytest.mark.parametrize("reader", ["worktree_root", "anchor"])
def test_a_read_resolves_each_directory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    """Keyed afresh at each use, the realpath keys made one read of a
    record about 400 ``lstat`` calls; the read keys each path once and
    resolves each directory once, so it makes at most one ``lstat`` per
    component of the root plus two per claimed path. The sparse-checkout
    probe a convicting miss consults reads the repository's files on its
    own account, and is stubbed out here."""
    root = Path(os.path.realpath(_tree(tmp_path.joinpath(*_DEEP))))
    present = [f"src/p{i}.py" for i in range(3)]
    for rel in present:
        (root / rel).write_text("x = 1\n")
    absolute = [str(root / rel) for rel in present] + [
        str(root / "src" / f"gone{i}.py") for i in range(3)
    ]
    relative = [f"src/r{i}.py" for i in range(4)]
    attested = ("src/a.py", "src/v.py")
    absent = ("src/gone.py",)
    body = " ".join(f"See `{p}`." for p in (*absolute, *relative))
    calls: list[str] = []
    real_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        calls.append(os.fsdecode(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(verify_anchoring, "_absent_by_sparsity", lambda _p: False)
    monkeypatch.setattr(os, "lstat", lstat)
    if reader == "anchor":
        anchor = ClaimAnchor(root=str(root), rule="home_checkout")
        report = detect_path_drift(
            body, verified_paths=attested, absent_paths=absent, anchor=anchor
        )
    else:
        report = detect_path_drift(
            body,
            verified_paths=attested,
            absent_paths=absent,
            worktree_root=str(root),
        )
    claims = len(absolute) + len(relative) + len(attested) + len(absent)
    assert len(report.checked) >= len(absolute), "premise: the claims were read"
    assert len(calls) <= len(root.parts) + 2 * claims, len(calls)
