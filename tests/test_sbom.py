"""`tools/sbom.py`: the CycloneDX SBOM built from uv.lock (a release asset
once release.yml runs it) and the license policy check `.github/workflows/
audit.yml` runs over the installed environment.

The SBOM is checked over the synthetic lock `tests/fixtures/osv/lock.toml`
and over the repository's own uv.lock. The license check is fed fake
metadata, never this interpreter's, so its verdicts do not depend on what
the test environment has installed.
"""

from __future__ import annotations

import datetime as dt
import email.message
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "osv"
POLICY = TOOLS / "license-policy.toml"


def _load(name: str) -> ModuleType:
    """A tools/ script as a module, registered under its own name so its
    dataclasses resolve and so a sibling's ``import _uvlock`` finds this
    copy rather than needing tools/ on sys.path."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


uvlock = _load("_uvlock")
sbom = _load("sbom")


@pytest.fixture(autouse=True)
def _no_source_date_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release job may export it; each test sets it only on purpose."""
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)


def _project(tmp_path: Path, *, version: str = "1.0.1", license_line: str = "") -> Path:
    """The fixture lock as a project's uv.lock, beside a pyproject.toml
    whose version differs from the lock's own entry (1.0.0), so a test can
    tell which one the SBOM read."""
    (tmp_path / "uv.lock").write_text(
        (FIXTURES / "lock.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    license_line = license_line or 'license = { text = "MIT" }'
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "{version}"\n{license_line}\n',
        encoding="utf-8",
    )
    return tmp_path / "uv.lock"


def _sbom(
    capsys: pytest.CaptureFixture[str], lock: Path, *args: str
) -> tuple[int, str]:
    code = sbom.main(["--lock", str(lock), *args])
    return code, capsys.readouterr().out


def _document(
    capsys: pytest.CaptureFixture[str], lock: Path, *args: str
) -> dict[str, Any]:
    code, out = _sbom(capsys, lock, *args)
    assert code == 0
    return json.loads(out)


PYPI = "pkg:pypi/{}"


# ------------------------------------------------------------ the SBOM


def test_two_runs_are_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No clock and no randomness: without SOURCE_DATE_EPOCH there is no
    timestamp, and with it the timestamp is that instant. Either way the
    same inputs give the same bytes, and the serial number moves with the
    content."""
    lock = _project(tmp_path)
    first = _sbom(capsys, lock)[1]
    assert first == _sbom(capsys, lock)[1]
    assert "timestamp" not in json.loads(first)["metadata"]

    epoch = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(epoch))
    stamped = _sbom(capsys, lock)[1]
    assert stamped == _sbom(capsys, lock)[1]
    document = json.loads(stamped)
    assert document["metadata"]["timestamp"] == "2026-01-01T00:00:00Z"
    assert document["serialNumber"] != json.loads(first)["serialNumber"]


def test_the_document_has_the_cyclonedx_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = _document(capsys, _project(tmp_path))
    assert list(document) == [
        "bomFormat",
        "specVersion",
        "serialNumber",
        "version",
        "metadata",
        "components",
        "dependencies",
    ]
    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.5"
    assert document["version"] == 1
    assert re.fullmatch(
        r"urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        document["serialNumber"],
    )
    assert document["metadata"]["tools"] == {
        "components": [
            {"type": "application", "group": "demo", "name": "tools/sbom.py"}
        ]
    }
    # The root is the project at pyproject's version, not the lock entry's.
    assert document["metadata"]["component"] == {
        "type": "application",
        "bom-ref": "pkg:pypi/demo@1.0.1",
        "name": "demo",
        "version": "1.0.1",
        "purl": "pkg:pypi/demo@1.0.1",
        "licenses": [{"license": {"id": "MIT"}}],
    }
    components = document["components"]
    assert [(c["name"], c["version"]) for c in components] == sorted(
        (c["name"], c["version"]) for c in components
    )
    refs = [c["bom-ref"] for c in components]
    assert len(set(refs)) == len(refs) == 12
    for component in components:
        assert component["type"] == "library"
        assert component["scope"] in ("required", "optional")
        references = component.get("externalReferences", [])
        assert [r["url"] for r in references] == sorted(r["url"] for r in references)
        for reference in references:
            assert reference["type"] == "distribution"
            (digest,) = reference["hashes"]
            assert digest["alg"] == "SHA-256"
            assert re.fullmatch(r"[0-9a-f]{64}", digest["content"])


def test_scope_says_runtime_or_dev(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = _document(capsys, _project(tmp_path))
    required = {
        (c["name"], c["version"])
        for c in document["components"]
        if c["scope"] == "required"
    }
    assert required == {
        ("webframe", "2.0.0"),
        ("internal-lib", "1.2.0"),
        ("parser", "1.0.0"),
        ("jwtlib", "3.1.0"),
        ("cryptokit", "42.0.0"),
        ("winshim", "306"),
        ("vendored", "0.1.0"),
    }
    optional = {
        (c["name"], c["version"])
        for c in document["components"]
        if c["scope"] == "optional"
    }
    assert optional == {
        ("testrunner", "8.0.0"),
        ("colorlib", "0.4.6"),
        ("parser", "2.0.0"),
        ("lintkit", "0.5.0"),
        ("orphan", "0.0.1"),
    }


def test_each_source_gets_the_reference_it_can_carry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PyPI: a plain purl. Another index: the purl names it. Git: no purl
    (none names a checkout), the source as a property. A wheel-only package
    has just its wheel."""
    by_name = {
        (c["name"], c["version"]): c
        for c in _document(capsys, _project(tmp_path))["components"]
    }
    webframe = by_name[("webframe", "2.0.0")]
    assert webframe["purl"] == webframe["bom-ref"] == "pkg:pypi/webframe@2.0.0"
    assert [r["hashes"][0]["content"][:4] for r in webframe["externalReferences"]] == [
        "cccc",
        "bbbb",
    ]
    assert by_name[("internal-lib", "1.2.0")]["purl"] == (
        "pkg:pypi/internal-lib@1.2.0?repository_url=https%3A%2F%2Fpkgs.example.invalid%2Fsimple"
    )
    vendored = by_name[("vendored", "0.1.0")]
    assert "purl" not in vendored
    assert "externalReferences" not in vendored
    assert vendored["bom-ref"] == "vendored@0.1.0"
    assert vendored["properties"] == [
        {
            "name": "uv:source",
            "value": "git:https://git.example.invalid/vendored.git"
            "?rev=main#0123456789abcdef0123456789abcdef01234567",
        }
    ]
    (wheel,) = by_name[("winshim", "306")]["externalReferences"]
    assert wheel["url"].endswith("win_amd64.whl")


def test_the_dependency_graph_follows_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The root first, then every component once. jwtlib depends on
    cryptokit because the root asked for its crypto extra, and each fork of
    parser hangs off the package that names its version."""
    document = _document(capsys, _project(tmp_path))
    graph = document["dependencies"]
    assert graph[0]["ref"] == "pkg:pypi/demo@1.0.1"
    edges = {entry["ref"]: entry["dependsOn"] for entry in graph}
    assert edges["pkg:pypi/demo@1.0.1"] == sorted(
        [
            PYPI.format("jwtlib@3.1.0"),
            PYPI.format("lintkit@0.5.0"),
            PYPI.format("testrunner@8.0.0"),
            PYPI.format("webframe@2.0.0"),
            PYPI.format("winshim@306"),
            "vendored@0.1.0",
        ]
    )
    assert edges[PYPI.format("jwtlib@3.1.0")] == [PYPI.format("cryptokit@42.0.0")]
    assert edges[PYPI.format("webframe@2.0.0")] == [
        "pkg:pypi/internal-lib@1.2.0?repository_url=https%3A%2F%2Fpkgs.example.invalid%2Fsimple",
        PYPI.format("parser@1.0.0"),
    ]
    assert edges[PYPI.format("testrunner@8.0.0")] == [
        PYPI.format("colorlib@0.4.6"),
        PYPI.format("parser@2.0.0"),
    ]
    assert edges[PYPI.format("orphan@0.0.1")] == []
    refs = {c["bom-ref"] for c in document["components"]}
    assert set(edges) == refs | {"pkg:pypi/demo@1.0.1"}
    assert all(set(targets) <= refs for targets in edges.values())


def test_scope_runtime_leaves_the_dev_packages_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = _document(capsys, _project(tmp_path), "--scope", "runtime")
    assert {c["scope"] for c in document["components"]} == {"required"}
    assert len(document["components"]) == 7
    root_edges = document["dependencies"][0]["dependsOn"]
    assert PYPI.format("testrunner@8.0.0") not in root_edges
    assert PYPI.format("lintkit@0.5.0") not in root_edges


def test_out_writes_the_same_bytes_and_makes_its_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = _project(tmp_path)
    printed = _sbom(capsys, lock)[1]
    out = tmp_path / "dist-extra" / "demo.cdx.json"
    code, stdout = _sbom(capsys, lock, "--out", str(out))
    assert code == 0
    assert stdout == ""
    # Bytes, not text: LF line endings on every platform, Windows included.
    assert out.read_bytes() == printed.encode("utf-8")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MIT", [{"license": {"id": "MIT"}}]),
        ("Apache-2.0 OR MIT", [{"expression": "Apache-2.0 OR MIT"}]),
        ("Acme-Internal", [{"license": {"name": "Acme-Internal"}}]),
        ("All rights (reserved", [{"license": {"name": "All rights (reserved"}}]),
        (None, []),
    ],
)
def test_the_root_license_is_written_in_the_form_the_schema_accepts(
    text: str | None, expected: list[dict[str, Any]]
) -> None:
    """An id from CycloneDX's SPDX enum, an expression, or a plain name."""
    assert sbom._root_license(text) == expected


def test_a_pyproject_without_a_license_writes_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = _project(tmp_path, license_line='description = "no license"')
    assert "licenses" not in _document(capsys, lock)["metadata"]["component"]


def test_a_dynamic_version_falls_back_to_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = _project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndynamic = ["version"]\n', encoding="utf-8"
    )
    assert _document(capsys, lock)["metadata"]["component"]["version"] == "1.0.0"


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("epoch", "SOURCE_DATE_EPOCH='yesterday'"),
        ("no-pyproject", "pyproject.toml"),
        ("no-lock", "uv.lock"),
    ],
)
def test_an_sbom_that_cannot_be_built_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    message: str,
) -> None:
    lock = _project(tmp_path)
    if setup == "epoch":
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    elif setup == "no-pyproject":
        (tmp_path / "pyproject.toml").unlink()
    else:
        lock.unlink()
    assert sbom.main(["--lock", str(lock)]) == 2
    assert message in capsys.readouterr().err


def test_the_repositorys_own_sbom(capsys: pytest.CaptureFixture[str]) -> None:
    """The release builds this one: the real lock reads, every package is
    listed once, the required ones are exactly the runtime closure, every
    one comes from PyPI with a purl and hashed files, and the root is the
    version pyproject.toml declares."""
    document = _document(capsys, REPO / "uv.lock")
    lock = uvlock.load(REPO / "uv.lock")
    name, version, _license = uvlock.project(REPO / "pyproject.toml")
    root = lock.root(name)
    runtime = {(key[0], key[1]) for key in lock.runtime(root)}
    components = document["components"]
    assert len(components) == len(lock.packages) - 1
    assert {
        (c["name"], c["version"]) for c in components if c["scope"] == "required"
    } == runtime
    for component in components:
        assert (
            component["purl"] == f"pkg:pypi/{component['name']}@{component['version']}"
        )
        assert component["externalReferences"]
    assert document["metadata"]["component"]["version"] == version
    assert document["metadata"]["component"]["licenses"] == [{"license": {"id": "MIT"}}]


@pytest.mark.skipif(
    os.environ.get("BETTERMEMORY_TEST_UV_EXPORT") != "1" or shutil.which("uv") is None,
    reason="compares with uv's preview SBOM export; set BETTERMEMORY_TEST_UV_EXPORT=1",
)
def test_the_runtime_set_matches_uvs_own_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """uv's exporter (a preview feature, so opt-in) writes the runtime
    closure too; the two must agree on which name and version pairs it
    holds."""
    out = tmp_path / "uv.cdx.json"
    uv = shutil.which("uv")
    assert uv is not None
    result = subprocess.run(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--preview-features",
            "sbom-export",
            "--format",
            "cyclonedx1.5",
            "-o",
            str(out),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"this uv cannot export an SBOM: {result.stderr.strip()[:200]}")
    theirs = {
        (c["name"], c["version"])
        for c in json.loads(out.read_text("utf-8"))["components"]
    }
    ours = _document(capsys, REPO / "uv.lock", "--scope", "runtime")["components"]
    assert {(c["name"], c["version"]) for c in ours} == theirs


# --------------------------------------------------------- the licenses


@pytest.mark.parametrize(
    ("text", "tree"),
    [
        ("MIT", ("id", "MIT", None)),
        (
            "MIT OR Apache-2.0",
            ("or", [("id", "MIT", None), ("id", "Apache-2.0", None)]),
        ),
        (
            "MIT OR GPL-3.0-only AND BSD-3-Clause",
            (
                "or",
                [
                    ("id", "MIT", None),
                    (
                        "and",
                        [("id", "GPL-3.0-only", None), ("id", "BSD-3-Clause", None)],
                    ),
                ],
            ),
        ),
        (
            "(MIT or GPL-3.0-only) and ISC",
            (
                "and",
                [
                    ("or", [("id", "MIT", None), ("id", "GPL-3.0-only", None)]),
                    ("id", "ISC", None),
                ],
            ),
        ),
        (
            "Apache-2.0 WITH LLVM-exception",
            ("id", "Apache-2.0", "LLVM-exception"),
        ),
    ],
)
def test_an_spdx_expression_parses_with_and_above_or(text: str, tree: Any) -> None:
    assert sbom.parse_expression(text) == tree


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Dual License",
        "MIT OR",
        "(MIT",
        "MIT)",
        "OR MIT",
        "MIT WITH",
        "Copyright © 2020",
    ],
)
def test_text_that_is_not_an_expression_is_refused(text: str) -> None:
    with pytest.raises(sbom.LicenseSyntaxError):
        sbom.parse_expression(text)


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("MIT", "allowed"),
        ("mit", "allowed"),
        ("BSD", "allowed"),
        ("Apache-2.0 OR BSD-3-Clause", "allowed"),
        ("MIT OR GPL-3.0-only", "allowed"),
        ("MIT AND GPL-3.0-only", "denied"),
        ("GPL-2.0+", "denied"),
        ("LGPL-2.1-or-later", "denied"),
        ("AGPL-3.0-only", "denied"),
        ("GPL-2.0-only WITH Classpath-exception-2.0", "denied"),
        ("Apache-2.0 WITH LLVM-exception", "allowed"),
        ("WTFPL", "unknown"),
        ("MIT AND WTFPL", "unknown"),
        ("WTFPL OR GPL-3.0-only", "unknown"),
        ("GPLv3-ish", "unknown"),
    ],
)
def test_the_repositorys_policy_judges(text: str, verdict: str) -> None:
    """OR needs one allowed branch, AND needs every part; a GPL-family id is
    denied whatever its suffix; anything off both lists is unknown."""
    policy = sbom.load_policy(POLICY)
    assert policy.judge(sbom.parse_expression(text)) == verdict


def test_the_repositorys_policy_records_the_platform_only_packages() -> None:
    """The runtime packages no Linux runner installs are recorded, each with
    a license the policy allows, so the check has nothing unchecked."""
    policy = sbom.load_policy(POLICY)
    for name in ("pywin32", "httpx2-jsfetch", "colorama"):
        rule = policy.packages[name]
        assert rule.reason
        assert rule.license is not None
        assert policy.judge(sbom.parse_expression(rule.license)) == "allowed"
        assert not rule.accept


def _meta(
    version: str = "1.0",
    *,
    expression: str | None = None,
    license_text: str | None = None,
    classifiers: tuple[str, ...] = (),
) -> email.message.Message:
    meta = email.message.Message()
    meta["Metadata-Version"] = "2.4"
    meta["Version"] = version
    if expression is not None:
        meta["License-Expression"] = expression
    if license_text is not None:
        meta["License"] = license_text
    for classifier in classifiers:
        meta["Classifier"] = classifier
    return meta


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (
            _meta(
                expression="MIT", license_text="BSD", classifiers=("License :: Other",)
            ),
            ("MIT", "License-Expression"),
        ),
        (_meta(license_text="Apache 2.0"), ("Apache-2.0", "License")),
        (_meta(license_text="BSD"), ("BSD", "License")),
        (_meta(license_text="BSD-3-Clause"), ("BSD-3-Clause", "License")),
        (_meta(license_text="MIT License"), ("MIT", "License")),
        (
            _meta(
                license_text="Copyright (c) 2020 Someone\n\nPermission is hereby granted",
                classifiers=("License :: OSI Approved :: MIT License",),
            ),
            ("MIT", "Classifier"),
        ),
        (
            _meta(
                license_text="UNKNOWN",
                classifiers=(
                    "License :: OSI Approved",
                    "License :: OSI Approved :: BSD License",
                    "License :: OSI Approved :: Apache Software License",
                    "Programming Language :: Python",
                ),
            ),
            ("BSD OR Apache-2.0", "Classifier"),
        ),
        (
            _meta(classifiers=("License :: Other/Proprietary License",)),
            ("LicenseRef-Other-Proprietary-License", "Classifier"),
        ),
        (
            _meta(
                classifiers=(
                    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
                )
            ),
            ("GPL-3.0", "Classifier"),
        ),
        (_meta(), (None, None)),
    ],
)
def test_the_declared_license_is_read_in_order(
    meta: Any, expected: tuple[Any, Any]
) -> None:
    """License-Expression, then a License field that names a license (a
    pasted license text does not), then the classifiers joined with OR."""
    assert sbom.declared(meta) == expected


def test_metadata_that_names_a_license_beats_the_policys_record() -> None:
    """A release that changes license is caught even where the policy
    recorded the old one."""
    policy = sbom.Policy(
        allow=frozenset({"MIT"}),
        deny=("GPL",),
        packages={"pkg": sbom.PackageRule(reason="was MIT", license="MIT")},
    )
    result = sbom.judge_package(
        policy, "pkg", "1.0", "runtime", _meta(expression="GPL-3.0-only")
    )
    assert (result.verdict, result.license, result.source) == (
        "denied",
        "GPL-3.0-only",
        "License-Expression",
    )
    assert result.failed


def test_the_policys_record_decides_where_metadata_cannot() -> None:
    policy = sbom.Policy(
        allow=frozenset({"MIT"}),
        deny=("GPL",),
        packages={"pkg": sbom.PackageRule(reason="reviewed by hand", license="MIT")},
    )
    unknown = sbom.judge_package(
        policy, "pkg", "1.0", "runtime", _meta(license_text="Custom")
    )
    assert (unknown.verdict, unknown.source) == ("allowed", "policy")
    assert unknown.note == "recorded by the policy: reviewed by hand"
    absent = sbom.judge_package(policy, "pkg", "1.0", "runtime", None)
    assert (absent.verdict, absent.license, absent.installed) == (
        "allowed",
        "MIT",
        None,
    )


def test_a_package_nothing_can_read_is_unchecked_and_fails() -> None:
    policy = sbom.Policy(allow=frozenset({"MIT"}), deny=("GPL",))
    result = sbom.judge_package(policy, "pkg", "1.0", "runtime", None)
    assert result.verdict == "unchecked"
    assert result.failed
    assert "not installed in this environment" in result.note


def test_accept_passes_a_denied_package_and_says_so() -> None:
    policy = sbom.Policy(
        allow=frozenset({"MIT"}),
        deny=("LGPL",),
        packages={"pkg": sbom.PackageRule(reason="dynamic linking only", accept=True)},
    )
    result = sbom.judge_package(
        policy, "pkg", "1.0", "runtime", _meta("1.1", expression="LGPL-3.0-or-later")
    )
    assert result.verdict == "accepted"
    assert not result.failed
    assert result.note == (
        "installed 1.1, the lock pins 1.0; accepted by the policy: dynamic linking only"
    )


def _lookup(table: dict[str, Any]) -> Any:
    return lambda name: table.get(name)


def test_the_check_covers_the_runtime_set_by_default() -> None:
    lock = uvlock.load(FIXTURES / "lock.toml")
    policy = sbom.Policy(allow=frozenset({"MIT"}), deny=("GPL",))
    table = {name: _meta(expression="MIT") for name in ("demo", "webframe", "jwtlib")}
    table["cryptokit"] = _meta(expression="GPL-3.0-only")
    results = sbom.check_licenses(lock, lock.root(), policy, lookup=_lookup(table))
    verdicts = {r.name: r.verdict for r in results}
    assert set(verdicts) == {
        "webframe",
        "internal-lib",
        "parser",
        "jwtlib",
        "cryptokit",
        "winshim",
        "vendored",
    }
    assert verdicts["webframe"] == "allowed"
    assert verdicts["cryptokit"] == "denied"
    assert verdicts["winshim"] == "unchecked"
    everything = sbom.check_licenses(
        lock, lock.root(), policy, scope="all", lookup=_lookup(table)
    )
    assert len(everything) == 12
    assert {r.set for r in everything} == {"runtime", "dev"}


def test_an_environment_without_the_project_is_refused() -> None:
    """Otherwise every package reads "not installed" and the check passes
    having read nothing: run with the system python3 instead of the venv."""
    lock = uvlock.load(FIXTURES / "lock.toml")
    policy = sbom.Policy(allow=frozenset({"MIT"}), deny=())
    with pytest.raises(sbom.UsageError, match="demo is not installed"):
        sbom.check_licenses(lock, lock.root(), policy, lookup=_lookup({}))


def test_the_license_cli_reports_and_exits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _project(tmp_path)
    policy = tmp_path / "policy.toml"
    policy.write_text(
        '[policy]\nallow = ["MIT", "BSD"]\ndeny = ["GPL"]\n'
        '[packages.winshim]\nlicense = "MIT"\nreason = "Windows only"\n'
        '[packages.vendored]\naccept = true\nreason = "our own fork"\n',
        encoding="utf-8",
    )
    table = {
        "demo": _meta(expression="MIT"),
        "webframe": _meta("2.0.0", expression="MIT"),
        "internal-lib": _meta("1.2.0", license_text="BSD License"),
        "parser": _meta(
            "1.0.0", classifiers=("License :: OSI Approved :: MIT License",)
        ),
        "jwtlib": _meta("3.1.0", expression="MIT"),
        "cryptokit": _meta("42.0.0", expression="MIT"),
    }
    monkeypatch.setattr(sbom, "installed_metadata", _lookup(table))
    code, out = _sbom(capsys, lock, "--licenses", "--policy", str(policy))
    assert code == 0, out
    assert out.startswith(
        f"licenses: 7 runtime packages from {lock}, policy {policy}\n"
    )
    assert re.search(r"ok +internal-lib 1\.2\.0 +\[runtime\] BSD \(License\)", out)
    assert re.search(
        r"ACCEPTED +vendored 0\.1\.0 .*accepted by the policy: our own fork", out
    )
    assert out.endswith("result: ok: 1 accepted, 6 allowed\n")

    table["cryptokit"] = _meta("42.0.0", expression="GPL-3.0-or-later")
    code, out = _sbom(
        capsys, lock, "--licenses", "--policy", str(policy), "--format", "json"
    )
    assert code == 1
    report = json.loads(out)
    assert report["status"] == "fail"
    assert report["scope"] == "runtime"
    (bad,) = [p for p in report["packages"] if p["verdict"] == "denied"]
    assert bad == {
        "package": "cryptokit",
        "version": "42.0.0",
        "set": "runtime",
        "installed": "42.0.0",
        "license": "GPL-3.0-or-later",
        "source": "License-Expression",
        "verdict": "denied",
        "note": "",
    }


def test_the_license_cli_refuses_a_foreign_interpreter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sbom, "installed_metadata", _lookup({}))
    assert sbom.main(["--lock", str(_project(tmp_path)), "--licenses"]) == 2
    assert (
        "is not installed in this interpreter's environment" in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "no \\[policy\\] table"),
        ("[policy]\nallow = []\n", "non-empty list"),
        ('[policy]\nallow = ["MIT"]\ndeny = "GPL"\n', "deny must be a list"),
        ('[policy]\nallow = ["MIT"]\n[packages.x]\nlicense = "MIT"\n', "has no reason"),
        (
            '[policy]\nallow = ["MIT"]\n[packages.x]\nreason = "r"\n',
            "neither license nor accept",
        ),
        (
            '[policy]\nallow = ["MIT"]\n[packages.x]\nreason = "r"\nlicence = "MIT"\n',
            "unknown keys",
        ),
        (
            '[policy]\nallow = ["MIT"]\n[packages.x]\nreason = "r"\nlicense = "MIT OR"\n',
            "license 'MIT OR'",
        ),
        (
            '[policy]\nallow = ["MIT"]\n[packages.x]\nreason = "r"\naccept = "yes"\n',
            "true or",
        ),
        ("not toml [", "policy.toml"),
    ],
)
def test_a_malformed_policy_is_refused(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(sbom.UsageError, match=message):
        sbom.load_policy(path)
