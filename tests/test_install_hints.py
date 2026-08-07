"""The install-command spelling exists once.

Three hand-written copies of one command string — ``doctor``'s fix
hints, the ``mode='semantic'`` error in ``handlers.search``, the
provider WARNING in ``semantic`` — are the drift that shipped an
unquoted, zsh-refused install command: each surface got fixed on its
own schedule, and each fix closed an instance rather than the class.
``bettermemory._install_hints`` closed the class by owning the atoms
every surface composes from. This module holds both halves of that
deal:

1. the atoms render the exact pastable spellings (users copy these into
   shells, so every byte — the quoting above all — is load-bearing), and
2. the spellings appear nowhere else in shipped source, so a fourth
   hand-written copy fails here instead of drifting.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from bettermemory import _install_hints

_REPO_ROOT = Path(__file__).resolve().parents[1]

_CANONICAL = "src/bettermemory/_install_hints.py"


def _tracked_python_sources() -> list[str]:
    """Repo-relative paths of tracked Python sources under `src/`.

    Same shape as ``test_doc_claims._tracked_python_sources``, for the
    same reason: `git ls-files` rather than `rglob`, so an untracked
    scratch file left in the tree cannot fail the suite.
    """
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "--", "src/*.py", "src/**/*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted({line for line in out.splitlines() if line.strip()})


# ---------------------------------------------------------------------------
# The atoms, verbatim
# ---------------------------------------------------------------------------


def test_atoms_render_the_pastable_spellings_verbatim() -> None:
    """Byte-for-byte pins, quoting included.

    The quoting IS the fix these spellings carry: `[` globs, so zsh
    refuses the unquoted spec outright. An atom that drifts by one
    quote reopens the original defect everywhere at once — the flip
    side of having one definition.
    """
    assert _install_hints.extras_spec("embeddings") == "'bettermemory[embeddings]'"
    assert (
        _install_hints.tool_reinstall("embeddings")
        == "uv tool install --reinstall 'bettermemory[embeddings]'"
    )
    assert (
        _install_hints.pipx_force("embeddings-fast")
        == "pipx install --force 'bettermemory[embeddings-fast]'"
    )
    assert (
        _install_hints.pip_force_reinstall("sentence_transformers")
        == "uv pip install --force-reinstall sentence_transformers"
    )
    assert _install_hints.dev_clone_editable("ui") == 'uv pip install -e ".[ui]"'


def test_composed_forms_keep_doctors_exact_shipped_shape() -> None:
    """The two convenience forms are what `doctor` shipped pre-extraction.

    `test_doctor`'s hint assertions (`"force-reinstall fastembed" in
    hint`, the parenthetical-position checks) read these through
    `_check_embeddings_extra`, so the extraction must be a pure move —
    byte-identical output, prose shape included.
    """
    assert _install_hints.install_extra_command("embeddings") == (
        "`uv tool install --reinstall 'bettermemory[embeddings]'` "
        "(pipx: `pipx install --force 'bettermemory[embeddings]'`; from a "
        'development clone: `uv pip install -e ".[embeddings]"`)'
    )
    assert _install_hints.reinstall_extra_command("fastembed", "embeddings-fast") == (
        "`uv tool install --reinstall 'bettermemory[embeddings-fast]'` "
        "(pipx: `pipx install --force 'bettermemory[embeddings-fast]'`; "
        "inside the virtualenv that runs bettermemory: "
        "`uv pip install --force-reinstall fastembed`)"
    )


def test_doctor_binds_the_composed_forms_not_copies() -> None:
    """`doctor`'s historical private names are the same function objects.

    Identity, not equality: a reintroduced local wrapper could stay
    string-equal today and drift tomorrow, which is the failure mode
    this module exists to end.
    """
    from bettermemory import doctor

    assert doctor._install_extra_command is _install_hints.install_extra_command
    assert doctor._reinstall_extra_command is _install_hints.reinstall_extra_command


# ---------------------------------------------------------------------------
# The ratchet
# ---------------------------------------------------------------------------

#: literal -> {repo-relative file: occurrence count} across tracked
#: `src/` sources. Shrink-only: migrating a listed site onto the atoms
#: updates its entry downward; a NEW copy anywhere is the regression
#: this pin exists to fail. Counts are of the raw source text, so a
#: spelling split across adjacent string fragments would evade the
#: grep — acceptable, because the defect class is drift-by-copy-paste,
#: and a paste lands contiguous.
_SPELLING_HOMES: dict[str, dict[str, int]] = {
    "uv tool install --reinstall": {_CANONICAL: 1},
    "pipx install --force": {_CANONICAL: 1},
    "--force-reinstall": {
        _CANONICAL: 1,
        # `doctor`'s distinfo-damage hint repairs arbitrary broken
        # packages, not an extra — a different message that shares the
        # flag (both spellings sit on one fix_hint, hence 2).
        "src/bettermemory/doctor.py": 2,
    },
    'install -e ".[': {_CANONICAL: 1},
}


@pytest.mark.parametrize(
    ("literal", "expected"),
    sorted(_SPELLING_HOMES.items()),
    ids=["force-reinstall-flag", "dev-clone-editable", "pipx-force", "tool-reinstall"],
)
def test_each_spelling_lives_only_in_its_pinned_homes(
    literal: str, expected: dict[str, int]
) -> None:
    # The canonical module is scanned unconditionally: `git ls-files`
    # does not list a file until it is first staged, and a ratchet that
    # skips its own anchor while unstaged pins nothing.
    found: dict[str, int] = {}
    for rel in sorted({*_tracked_python_sources(), _CANONICAL}):
        count = (_REPO_ROOT / rel).read_text(encoding="utf-8").count(literal)
        if count:
            found[rel] = count
    assert found == expected, (
        f"{literal!r} drifted: expected {expected}, found {found}. A new "
        f"occurrence belongs in {_CANONICAL} (compose the message from "
        "its atoms); a vanished one means shrink the pin here."
    )


def test_install_hints_stays_import_free() -> None:
    """Zero imports, `__future__` included — pinned because it is relied on.

    `handlers.search` imports the module at module level and `semantic`
    lazily on its failure path; both placements were justified by the
    import being unconditionally cheap, which only holds while this
    module pulls in nothing at all.
    """
    source = (_REPO_ROOT / _CANONICAL).read_text(encoding="utf-8")
    imports = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert imports == []
