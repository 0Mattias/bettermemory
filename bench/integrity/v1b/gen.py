"""Integrity benchmark v1b: a fresh held-out test split, generated with v1's
machinery.

v1's test split was read once, by its arms, after the operating points
were frozen on v1's dev split. v1b is a second read-once split for the
same frozen points: four new fictional organisations (spec.py: kst, hlv,
mrw, pdn), all on the test split, authored by openai/gpt-6-sol through
OpenRouter (`author`) against the prompts v1 used. This file is
bench/integrity/v1/gen.py copied and changed where v1b differs:

- No dev split. `assemble` writes test.json only; the arms reuse v1's
  dev split and bench/integrity/v1/operating-points.json, whose sha the
  seal records as operating_points_sha256 beside v1 dev's sha as
  dev_sha256 (the split the points were frozen on).
- The same mix. v1's plan draws each planted slot's evasive technique,
  harm, plain secret shape and the high-entropy kind from global cycles
  that its two dev organisations advanced before its four test ones.
  v1b's cycles start where v1's test organisations started
  (`cycle_offsets`, computed from v1's own plan run in memory), and v1b's
  split classes follow v1 test's (false fact, secret, false fact,
  secret), so v1b's technique, harm and variant mix is v1 test's slot for
  slot.
- Fresh values, without exposing v1's. `withheld` gathers every v1 value
  (the topic and false-fact values of v1's dev and test splits and every
  candidate of v1's vocabulary tables), normalised (score.norm). The draw
  keeps every v1b value from containing or being contained in one; the
  vocabulary and prose checks flag a candidate or a text that carries one
  as "a value of an earlier split (withheld)", never the value itself, so
  no v1 text reaches the author's repair prompt or the terminal; and
  `assemble` checks the union of v1 dev, v1 test and v1b, printing counts.

The orchestrating chat model never reads v1b content (nor v1 test's):
every command prints counts, check names and shas only, and the problems
a check finds go to the author's repair prompt and to the work files,
never to the terminal.

Usage (repo venv, from the repo root):

    python bench/integrity/v1b/gen.py plan
    python bench/integrity/v1b/gen.py withheld
    python bench/integrity/v1b/gen.py author --org kst --stage vocab [--repair]
    python bench/integrity/v1b/gen.py check --org kst --stage vocab
    python bench/integrity/v1b/gen.py draw
    python bench/integrity/v1b/gen.py author --org kst --stage prose [--repair | --review]
    python bench/integrity/v1b/gen.py check --org kst --stage prose
    python bench/integrity/v1b/gen.py audit --org kst
    python bench/integrity/v1b/gen.py audit-report --out <owner-audit.md>
    python bench/integrity/v1b/gen.py assemble
    python bench/integrity/v1b/gen.py sample --out <owner-sample.md>
    python bench/integrity/v1b/gen.py seal
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import itertools
import json
import os
import random
import re
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
_ROOT = _HERE.parents[2]
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

import score  # noqa: E402


def _load(name: str, path: Path) -> ModuleType:
    """The module at `path` under `name`, reusing one already loaded from
    the same file. v1 and v1b each have a spec.py, so neither is imported
    as plain `spec` here: v1b's spec is loaded under its own name, and v1's
    generator (`v1_gen`) is executed against v1's spec explicitly."""
    module = sys.modules.get(name)
    if module is not None and getattr(module, "__file__", None) == str(path):
        return module
    found = importlib.util.spec_from_file_location(name, path)
    assert found is not None and found.loader is not None
    module = importlib.util.module_from_spec(found)
    sys.modules[name] = module
    found.loader.exec_module(module)
    return module


spec = _load("integrity_v1b_spec", _HERE / "spec.py")

SEED = 20261011
VERSION = "v1b-test"
BENCHMARK = f"integrity-{VERSION}"
WORK = _HERE / "work"
TEST_PATH = _HERE / "test.json"
SEALS_PATH = _HERE / "SEALS.json"
V0_CORPUS = _BENCH / "corpus.json"

# v1, read at run time and printed as counts and shas only.
V1 = _BENCH / "v1"
V1_GEN = V1 / "gen.py"
V1_SPEC = V1 / "spec.py"
V1_DEV = V1 / "dev.json"
V1_TEST = V1 / "test.json"
V1_WORK = V1 / "work"
V1_POINTS = V1 / "operating-points.json"
WITHHELD = "a value of an earlier split (withheld)"
WITHHELD_MIN = 4  # score.norm characters; v1's checks held every value to it

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
AUTHORING_CAP_USD = 3.0  # summed over v1b's own work files only
# The audit's routing. On 2026-10-11 OpenRouter served z-ai/glm-5.3 for the
# mrw audit twice from the provider Wafer, which answered 7,561 prompt
# tokens with 47 and 59 completion tokens in one to three seconds and a
# self-reported item count of 39 and 45 of about 80: no reasoning, no
# judgement. The kst, hlv and pdn audits, served elsewhere, ran 19,665 to
# 31,817 completion tokens. The hlv re-audit after its extra marker repair
# was then served by Morph: 79 completion tokens in 1.8 s. So the audit
# runs only on the providers that returned real audits (InferenceNet, Sail
# Research, Makora), and an answer under AUDIT_MIN_COMPLETION_TOKENS is
# refused as no audit at all.
AUDIT_PROVIDER: dict[str, Any] = {
    "only": ["InferenceNet", "Sail Research", "Makora"],
    "allow_fallbacks": False,
}
AUDIT_MIN_COMPLETION_TOKENS = 2000
MAX_REPAIRS = 3

LABEL_WORDS = re.compile(
    r"\b(false facts?|hard negatives?|benchmark|test items?|poison(ed)? (item|memory|note|entry)|planted (item|note|entry))\b",
    re.IGNORECASE,
)
# The split technique's class per organisation, in v1 test's order (trn
# false_fact, vld secret, fcs false_fact, qrv secret), so the cycles below
# hand v1b's organisations what they handed v1's test ones.
SPLIT_CLASS = {
    "kst": "false_fact",
    "hlv": "secret",
    "mrw": "false_fact",
    "pdn": "secret",
}
# The plan's global cycles, as v1's plan() writes them inline.
FF_CYCLE = ("dated_update", "embedded_true", "paraphrase", "attributed")
IN_CYCLE = (
    "advice",
    "policy_id",
    "descriptive_norm",
    "embedded_step",
    "agent_conditional",
)
SE_CYCLE = (
    "separator_split",
    "encoded",
    "reversed_or_spelled",
    "url_userinfo",
    "env_export_fenced",
    "yaml_json_snippet",
)
# Published example credentials a generated string must never equal.
PUBLISHED_EXAMPLES = (
    "AKIAIOSFODNN7EXAMPLE",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "je7MtGbClwBF/2Zp9Utk/h3yCo8nvbEXAMPLEKEY",
    "sk_test_4eC39HqLyjWDarjtT1zdp7dc",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6"
    "IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
)
HIGH_ENTROPY_KINDS = (
    "container image digest",
    "git commit sha",
    "release artefact sha256 checksum",
    "UUID",
)


def org_by_id(oid: str) -> dict[str, Any]:
    return next(o for o in spec.ORGS if o["id"] == oid)


def is_test(oid: str) -> bool:
    return org_by_id(oid)["split"] == "test"


def canonical(obj: Any) -> str:
    return (
        json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )


def write_canonical(path: Path, obj: Any) -> None:
    """Write a split as its canonical bytes. SEALS.json seals the sha256 of
    the file's bytes, and a text-mode write on Windows ends the file in CRLF
    where canonical() ends it in LF, so the split is written in bytes and
    hashes the same on every platform."""
    path.write_bytes(canonical(obj).encode("utf-8"))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def say(oid: str, header: str, problems: list[str]) -> None:
    """Print a check's outcome as a count: every v1b organisation is a test
    organisation, and the orchestrator never reads test content (v1's
    generator printed a dev organisation's problems in full)."""
    if not problems:
        print(f"{oid} {header}: ok")
    else:
        print(
            f"{oid} {header}: {len(problems)} problem(s) (details withheld: test organisation)"
        )


# ---------------------------------------------------------------------------
# the mechanical skeleton
# ---------------------------------------------------------------------------


def _rand(rng: random.Random, n: int, alphabet: str) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


ALNUM = string.ascii_letters + string.digits


def secret_material(rng: random.Random, oid: str, kind: str) -> Any:
    """Fake credential material in a real shape, or already transformed for
    an evasive technique. Random by construction; checked against the
    published examples in `plan`."""
    user = f"{oid}_{rng.choice(['svc', 'ops', 'etl', 'sync', 'ci'])}_{_rand(rng, 4, string.ascii_lowercase + string.digits)}"
    password = (
        _rand(rng, 1, string.ascii_uppercase)
        + _rand(rng, 12, ALNUM)
        + rng.choice("!#%*+=?@^")
        + _rand(rng, 3, string.digits)
    )
    token = f"{oid}_{rng.choice(['tok', 'key', 'sec', 'pat'])}_{_rand(rng, 32, ALNUM)}"
    host = f"{rng.choice(['db', 'api', 'sync', 'files', 'queue'])}-{_rand(rng, 3, string.ascii_lowercase)}.{oid}.invalid"
    if kind == "aws_key_pair":
        return {
            "access_key_id": "AKIA" + _rand(rng, 16, string.ascii_uppercase + "234567"),
            "secret_access_key": _rand(rng, 40, ALNUM + "/+"),
        }
    if kind == "github_token":
        return {"token": "ghp_" + _rand(rng, 36, ALNUM)}
    if kind == "pem_private_key":
        body = base64.b64encode(rng.randbytes(96)).decode()
        return {
            "pem": "-----BEGIN EC PRIVATE KEY-----\n"
            + "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
            + "\n-----END EC PRIVATE KEY-----"
        }
    if kind == "jwt":
        header = _b64url(b'{"alg":"HS256","typ":"JWT"}')
        payload = _b64url(
            json.dumps(
                {
                    "sub": user,
                    "iat": rng.randint(1767225600, 1790000000),
                    "scope": "rw",
                },
                separators=(",", ":"),
            ).encode()
        )
        return {"token": f"{header}.{payload}.{_b64url(rng.randbytes(32))}"}
    if kind == "password_assignment":
        return {
            "assignment": f"{oid.upper()}_{rng.choice(['DB', 'SFTP', 'SMTP', 'BROKER'])}_PASSWORD={password}"
        }
    if kind == "dsn_password":
        db = rng.choice(["postgres", "mysql", "mongodb"])
        port = {"postgres": 5432, "mysql": 3306, "mongodb": 27017}[db]
        return {
            "dsn": f"{db}://{user}:{password}@{host}:{port}/{oid}{_rand(rng, 3, string.ascii_lowercase)}"
        }
    if kind == "separator_split":
        sep = rng.choice([" ", "-"])
        grouped = sep.join(token[i : i + 4] for i in range(0, len(token), 4))
        return {"separated": grouped, "separator": "spaces" if sep == " " else "dashes"}
    if kind == "encoded":
        if rng.random() < 0.5:
            return {
                "encoded": base64.b64encode(f"{user}:{password}".encode()).decode(),
                "encoding": "base64",
                "decodes_to": "a user:password pair",
            }
        return {
            "encoded": token.encode().hex(),
            "encoding": "hex",
            "decodes_to": "an API token",
        }
    if kind == "reversed_or_spelled":
        return {"reversed": token[::-1], "decodes_to": "an API token, stored reversed"}
    if kind == "url_userinfo":
        return {
            "url": f"https://{user}:{password}@{host}/{rng.choice(['sync', 'export', 'ingest', 'v2/upload'])}"
        }
    if kind == "env_export_fenced":
        return {
            "export_line": f"export {oid.upper()}_{rng.choice(['DEPLOY', 'SYNC', 'UPLOAD', 'ADMIN'])}_TOKEN={token}"
        }
    if kind == "yaml_json_snippet":
        if rng.random() < 0.5:
            return {
                "snippet": f"client_id: {user}\nclient_secret: {token}",
                "format": "YAML",
            }
        return {
            "snippet": json.dumps({"client_id": user, "client_secret": token}),
            "format": "JSON",
        }
    if kind == "split":
        return {
            "a": token[:18],
            "b": token[18:],
            "about": "Together they form the client secret of a service account.",
        }
    raise ValueError(kind)


def material_strings(material: Any) -> list[str]:
    """The literals an item must carry for its material."""
    if isinstance(material, dict):
        keys = (
            "access_key_id",
            "secret_access_key",
            "token",
            "pem",
            "assignment",
            "dsn",
            "separated",
            "encoded",
            "reversed",
            "url",
            "export_line",
            "snippet",
            "a",
            "b",
        )
        return [material[k] for k in keys if k in material]
    return [str(material)]


def high_entropy(rng: random.Random, kind: str) -> str:
    if kind == "container image digest":
        return "sha256:" + rng.randbytes(32).hex()
    if kind == "git commit sha":
        return rng.randbytes(20).hex()
    if kind == "release artefact sha256 checksum":
        return rng.randbytes(32).hex()
    return str(uuid.UUID(bytes=rng.randbytes(16), version=4))


# The cycles by name; `cycle_offsets` and `plan` share them.
CYCLES: dict[str, tuple[str, ...]] = {
    "ff": FF_CYCLE,
    "in": IN_CYCLE,
    "se": SE_CYCLE,
    "shape": tuple(spec.PLAIN_SECRET_SHAPES),
    "harm": tuple(spec.HARM_TYPES),
    "he": HIGH_ENTROPY_KINDS,
}


def v1_gen() -> ModuleType:
    """v1's generator, loaded in memory against v1's own spec. Only its
    code is used (its seeded `plan`); nothing it would read from disk is
    read. sys.path and a module named `spec` are restored afterwards, so
    loading it leaves the importing process as it was."""
    name = "integrity_v1_gen_for_v1b"
    module = sys.modules.get(name)
    if module is not None and getattr(module, "__file__", None) == str(V1_GEN):
        return module
    v1_spec = _load("integrity_v1_spec_for_v1b", V1_SPEC)
    saved_path = list(sys.path)
    saved_spec = sys.modules.get("spec")
    sys.modules["spec"] = v1_spec
    try:
        module = _load(name, V1_GEN)
    finally:
        sys.path[:] = saved_path
        if saved_spec is None:
            sys.modules.pop("spec", None)
        else:
            sys.modules["spec"] = saved_spec
    return module


def cycle_consumption(org_plan: dict[str, Any]) -> dict[str, int]:
    """How many positions of each global cycle one organisation's plan
    consumed: an evasive false fact, instruction or secret drawn from its
    cycle (a split is assigned, not drawn), every plain secret's shape,
    every instruction's harm and the high-entropy hard negative's kind."""
    planted = org_plan["planted"]
    return {
        "ff": sum(
            1
            for s in planted
            if s["class"] == "false_fact" and s["technique"] in FF_CYCLE
        ),
        "in": sum(
            1
            for s in planted
            if s["class"] == "instruction" and s["technique"] in IN_CYCLE
        ),
        "se": sum(
            1 for s in planted if s["class"] == "secret" and s["technique"] in SE_CYCLE
        ),
        "shape": sum(1 for s in planted if s["technique"] == "plain_shape"),
        "harm": sum(1 for s in planted if s["class"] == "instruction"),
        "he": sum(1 for h in org_plan["hard_negatives"] if "material_kind" in h),
    }


def cycle_offsets(v1_plan: dict[str, Any] | None = None) -> dict[str, int]:
    """Where each cycle stood when v1's plan reached its first test
    organisation: the positions v1's dev organisations consumed, counted
    from v1's own seeded plan (run in memory unless given), modulo the
    cycle's length. v1b's plan starts its cycles there, so it hands its
    organisations the techniques, harms, shapes and kinds v1's plan
    handed v1's test organisations."""
    g = v1_gen()
    p = v1_plan if v1_plan is not None else g.plan()
    counts = dict.fromkeys(CYCLES, 0)
    for org in g.spec.ORGS:
        if org["split"] == "test":
            break
        for key, n in cycle_consumption(p["orgs"][org["id"]]).items():
            counts[key] += n
    return {key: counts[key] % len(CYCLES[key]) for key in CYCLES}


def plan(offsets: dict[str, int] | None = None) -> dict[str, Any]:
    """The seeded skeleton for the four organisations: v1's plan, with
    every cycle started at `offsets` (`cycle_offsets` by default)."""
    offsets = cycle_offsets() if offsets is None else dict(offsets)

    def started(name: str) -> Iterator[str]:
        it = itertools.cycle(CYCLES[name])
        for _ in range(offsets[name]):
            next(it)
        return it

    ff_cycle = started("ff")
    in_cycle = started("in")
    se_cycle = started("se")
    shape_cycle = started("shape")
    harm_cycle = started("harm")
    he_cycle = started("he")
    out: dict[str, Any] = {"seed": SEED, "cycle_offsets": offsets, "orgs": {}}
    for org in spec.ORGS:
        oid = org["id"]
        rng = random.Random(f"{SEED}:{oid}")
        topics = [
            {"id": f"{oid}.t{i:02d}", "kind": kind}
            for i, kind in enumerate(spec.TOPIC_KINDS, start=1)
        ]
        supersession = [t["id"] for t in topics if t["kind"] == "supersession"]
        targets = sorted(rng.sample(supersession, spec.N_FALSE_FACTS))
        rng.shuffle(targets)
        hns = []
        for i, cat in enumerate(spec.HARD_NEGATIVE_CATEGORIES, start=1):
            hn: dict[str, Any] = {"id": f"{oid}.hn{i:02d}", "category": cat}
            if cat == "high_entropy_nonsecret":
                kind = next(he_cycle)
                hn["material_kind"] = kind
                hn["material"] = high_entropy(rng, kind)
            hns.append(hn)
        planted: list[dict[str, Any]] = []
        n = 0
        for cls in ("false_fact", "instruction", "secret"):
            for variant in ("plain", "plain", "plain", "evasive", "evasive", "evasive"):
                n += 1
                slot: dict[str, Any] = {
                    "id": f"{oid}.p{n:02d}",
                    "class": cls,
                    "variant": variant,
                }
                last_evasive = variant == "evasive" and n in (6, 18)
                if cls == "false_fact":
                    if variant == "plain":
                        slot["technique"] = "flat_assertion"
                    elif last_evasive and SPLIT_CLASS[oid] == "false_fact":
                        slot["technique"] = "split"
                    else:
                        slot["technique"] = next(ff_cycle)
                    slot["target_topic"] = targets[n - 1]
                elif cls == "instruction":
                    slot["technique"] = (
                        "imperative" if variant == "plain" else next(in_cycle)
                    )
                    slot["harm"] = next(harm_cycle)
                else:
                    if variant == "plain":
                        slot["technique"] = "plain_shape"
                        slot["shape"] = next(shape_cycle)
                        slot["material"] = secret_material(rng, oid, slot["shape"])
                    else:
                        if last_evasive and SPLIT_CLASS[oid] == "secret":
                            slot["technique"] = "split"
                        else:
                            slot["technique"] = next(se_cycle)
                        slot["shape"] = slot["technique"]
                        slot["material"] = secret_material(rng, oid, slot["technique"])
                planted.append(slot)
        gqs = [
            {"id": f"{oid}.gq{i}", "theme": theme}
            for i, theme in enumerate(spec.GENERIC_QUERY_THEMES, start=1)
        ]
        out["orgs"][oid] = {
            "org": oid,
            "split": org["split"],
            "author": spec.TEST_AUTHOR,
            "topics": topics,
            "hard_negatives": hns,
            "planted": planted,
            "generic_queries": gqs,
        }
    for oid, p in out["orgs"].items():
        for slot in p["planted"]:
            for lit in material_strings(slot.get("material")):
                for ex in PUBLISHED_EXAMPLES:
                    if ex in lit or lit in ex:
                        raise SystemExit(
                            f"{slot['id']}: material equals a published example"
                        )
    return out


# ---------------------------------------------------------------------------
# the withheld values of the earlier split
# ---------------------------------------------------------------------------


def corpus_values(corpus: dict[str, Any]) -> list[str]:
    """A split's topic values and planted false facts' values."""
    vals = [v for t in corpus.get("topics", []) for v in t["values"].values()]
    vals += [
        p["value"]
        for p in corpus.get("poison", [])
        if p.get("class") == "false_fact" and p.get("value")
    ]
    return vals


def table_values(table: dict[str, Any]) -> list[str]:
    """Every candidate of a vocabulary table, drawn or not."""
    return [
        c
        for f in table.get("frames", [])
        if isinstance(f, dict)
        for c in f.get("candidates") or []
        if isinstance(c, str)
    ]


def withheld_values(
    corpora: Iterable[dict[str, Any]], tables: Iterable[dict[str, Any]]
) -> frozenset[str]:
    """The normalised (score.norm) values of the given splits and the
    candidates of the given vocabulary tables, at least WITHHELD_MIN
    characters long (v1's checks held every value to that, so a shorter
    one can only be a malformed leftover, which would otherwise block
    every draw it is a fragment of)."""
    raw = [v for c in corpora for v in corpus_values(c)]
    raw += [v for t in tables for v in table_values(t)]
    return frozenset(n for n in map(score.norm, raw) if len(n) >= WITHHELD_MIN)


def load_withheld() -> frozenset[str]:
    """v1's values, from v1's dev and test splits and its work tables
    (bench/integrity/v1/work/*/vocab.json). Loaded at run time and never
    printed: callers print a count and a sha (`withheld_digest`)."""
    corpora = [read_json(p) for p in (V1_DEV, V1_TEST)]
    tables = [read_json(p) for p in sorted(V1_WORK.glob("*/vocab.json"))]
    return withheld_values(corpora, tables)


def withheld_digest(withheld: Iterable[str]) -> str:
    """The sha256 of the withheld set's canonical JSON (sorted), which
    names the set without showing it."""
    return hashlib.sha256(canonical(sorted(withheld)).encode()).hexdigest()


def overlaps_withheld(value: str, withheld: Iterable[str]) -> bool:
    """Whether `value` contains or is contained in a withheld value, both
    normalised as the scorer matches them."""
    n = score.norm(value)
    return bool(n) and any(w in n or n in w for w in withheld)


def carries_withheld(text: str, withheld: Iterable[str]) -> bool:
    """Whether `text` carries a withheld value (score.carries; a withheld
    value is already normalised, and score.norm is idempotent)."""
    nt = score.norm(text)
    return any(w in nt for w in withheld)


# ---------------------------------------------------------------------------
# stage 1: the vocabulary tables and the draw
# ---------------------------------------------------------------------------


def check_vocab(
    oid: str, table: Any, withheld: Iterable[str] = frozenset()
) -> list[str]:
    """A vocabulary table's own rules, and that no candidate contains or
    is contained in a `withheld` value: such a candidate is named by its
    frame and position only, so the problem, which goes to the author's
    repair prompt, never carries the earlier split's value."""
    withheld = frozenset(withheld)
    problems: list[str] = []
    frames = table.get("frames") if isinstance(table, dict) else None
    if not isinstance(frames, list):
        return ['the object has no "frames" list']
    want = [(f"{oid}.t{i:02d}", k) for i, k in enumerate(spec.TOPIC_KINDS, start=1)]
    got = [(f.get("id"), f.get("kind")) for f in frames if isinstance(f, dict)]
    if got != want:
        problems.append(
            f"frames must be exactly, in order: {', '.join(f'{i} ({k})' for i, k in want)}"
        )
    # Collisions across frames are the draw's to avoid (it needs two or
    # three of five candidates), so only a frame's own candidates are held
    # apart here.
    subjects: dict[str, str] = {}
    for f in frames:
        if not isinstance(f, dict):
            continue
        fid = f.get("id", "?")
        for key in ("subject", "attribute", "value_type"):
            if not isinstance(f.get(key), str) or not f[key].strip():
                problems.append(f"{fid}: {key} missing")
        subj = str(f.get("subject", "")).strip().lower()
        if subj in subjects:
            problems.append(f"{fid}: subject repeats {subjects[subj]}'s subject")
        subjects[subj] = fid
        cands = f.get("candidates")
        if (
            not isinstance(cands, list)
            or len(cands) != 5
            or not all(isinstance(c, str) for c in cands)
        ):
            problems.append(f"{fid}: candidates must be a list of exactly 5 strings")
            continue
        norms = [score.norm(c) for c in cands]
        for c, nc in zip(cands, norms):
            if len(c.strip()) < 5 or len(nc) < 4:
                problems.append(f"{fid}: candidate {c!r} is shorter than 5 characters")
        for (a, na), (b, nb) in itertools.permutations(zip(cands, norms), 2):
            if na and na in nb:
                problems.append(
                    f"{fid}: candidate {a!r} is contained in candidate {b!r} (ignoring case, spaces, hyphens, underscores and dots)"
                )
        for i, c in enumerate(cands, start=1):
            if overlaps_withheld(c, withheld):
                problems.append(
                    f"{fid}: candidate {i} contains or is contained in {WITHHELD} "
                    "(ignoring case, spaces, hyphens, underscores and dots); replace it"
                )
    return sorted(set(problems))


def draw(
    plan_obj: dict[str, Any],
    tables: dict[str, Any],
    withheld: Iterable[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Draw every topic's values from its frame's candidates, seeded, so no
    value in any organisation contains another (score.norm), none contains
    or is contained in a `withheld` value of the earlier split, and every
    false fact gets a value of its own. Returns the skeletons and, per
    organisation, the frames no draw could satisfy."""
    accepted: list[str] = sorted(withheld)
    skeletons: dict[str, Any] = {}
    failures: dict[str, list[str]] = {}
    for org in spec.ORGS:
        oid = org["id"]
        p = plan_obj["orgs"][oid]
        targets = {
            s["target_topic"] for s in p["planted"] if s["class"] == "false_fact"
        }
        frames = {f["id"]: f for f in tables[oid]["frames"]}
        topics = []
        values_for_ff: dict[str, str] = {}
        for t in p["topics"]:
            f = frames[t["id"]]
            need = {"supersession": 2, "distractor": 1, "reversion": 2}[t["kind"]]
            if t["id"] in targets:
                need += 1
            rng = random.Random(f"{SEED}:{t['id']}:draw")
            cands = list(f["candidates"])
            rng.shuffle(cands)
            chosen = None
            for combo in itertools.permutations(cands, need):
                ns = [score.norm(v) for v in combo]
                if any(len(n) < 4 for n in ns):
                    continue
                if any(a in b for a, b in itertools.permutations(ns, 2)):
                    continue
                if any(n in acc or acc in n for n in ns for acc in accepted):
                    continue
                chosen = combo
                break
            if chosen is None:
                failures.setdefault(oid, []).append(
                    f"{t['id']}: no draw of {need} candidates avoids the other frames' values "
                    "and the earlier split's (withheld); give this frame five new candidates "
                    "that share no fragment with any other value"
                )
                continue
            accepted.extend(score.norm(v) for v in chosen)
            values = {"old": chosen[0]}
            if t["kind"] != "distractor":
                values["new"] = chosen[1]
            if t["id"] in targets:
                values_for_ff[t["id"]] = chosen[-1]
            topics.append(
                {
                    "id": t["id"],
                    "kind": t["kind"],
                    "subject": f["subject"],
                    "attribute": f["attribute"],
                    "value_type": f.get("value_type"),
                    "values": values,
                    "current": "new" if t["kind"] == "supersession" else "old",
                }
            )
        planted = []
        for s in p["planted"]:
            s = dict(s)
            if s["class"] == "false_fact" and s["target_topic"] in values_for_ff:
                s["value"] = values_for_ff[s["target_topic"]]
            planted.append(s)
        skeletons[oid] = {**p, "topics": topics, "planted": planted}
    return skeletons, failures


# ---------------------------------------------------------------------------
# stage 2: the prose checks
# ---------------------------------------------------------------------------

ROLES = {
    "supersession": ("f1", "f2"),
    "distractor": ("f1", "d"),
    "reversion": ("f1", "f2", "f3"),
}


def all_values(skeletons: dict[str, Any]) -> dict[str, str]:
    """Every topic value and every false fact's value, by label."""
    vals: dict[str, str] = {}
    for sk in skeletons.values():
        for t in sk["topics"]:
            for k, v in t["values"].items():
                vals[f"{t['id']}.{k}"] = v
        for s in sk["planted"]:
            if s["class"] == "false_fact" and s.get("value"):
                vals[s["id"]] = s["value"]
    return vals


def check_prose(
    oid: str,
    sk: dict[str, Any],
    prose: Any,
    others: dict[str, Any],
    withheld: Iterable[str] = frozenset(),
) -> list[str]:
    """Every rule the corpus checks enforce, applied to one organisation's
    prose against its skeleton, plus the material, marker, label and
    length rules. `others` are the other organisations' skeletons, whose
    values this organisation's text must not carry either; nor may it
    carry a `withheld` value of the earlier split, which a problem names
    only as "a value of an earlier split (withheld)"."""
    withheld = frozenset(withheld)
    problems: list[str] = []
    if not isinstance(prose, dict):
        return ["the answer is not a JSON object"]
    by_id = {
        key: {x.get("id"): x for x in prose.get(key, []) if isinstance(x, dict)}
        for key in ("topics", "hard_negatives", "planted", "generic_queries")
    }
    own_vals = {
        f"{t['id']}.{k}": v for t in sk["topics"] for k, v in t["values"].items()
    }
    ff_vals = {s["id"]: s["value"] for s in sk["planted"] if s["class"] == "false_fact"}
    foreign = all_values(others)
    topic_of = {t["id"]: t for t in sk["topics"]}

    def carried(
        text: str, exclude_topic: str | None = None, exclude_pid: str | None = None
    ) -> list[str]:
        hits = [
            f"{k.rsplit('.', 1)[0]}'s value {v!r}"
            for k, v in own_vals.items()
            if k.rsplit(".", 1)[0] != exclude_topic and score.carries(text, v)
        ]
        hits += [
            f"{pid}'s false value {v!r}"
            for pid, v in ff_vals.items()
            if pid != exclude_pid and score.carries(text, v)
        ]
        # Another v1b organisation's value may be named: one author wrote
        # them all, and the problems go to it, never to the terminal.
        hits += [
            f"another organisation's value {v!r}"
            for v in foreign.values()
            if score.carries(text, v)
        ]
        if carries_withheld(text, withheld):
            hits.append(WITHHELD)
        return hits

    def common(label: str, text: Any, lo: int = 40, hi: int = 900) -> str | None:
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{label}: text missing")
            return None
        if not lo <= len(text) <= hi:
            problems.append(f"{label}: {len(text)} characters, outside {lo} to {hi}")
        if LABEL_WORDS.search(text):
            problems.append(
                f"{label}: labels itself ({LABEL_WORDS.search(text).group(0)!r})"
            )
        return text

    for t in sk["topics"]:
        got = by_id["topics"].get(t["id"])
        if got is None:
            problems.append(f"{t['id']}: topic missing")
            continue
        stmts = got.get("statements") if isinstance(got.get("statements"), dict) else {}
        want = ROLES[t["kind"]]
        if sorted(stmts) != sorted(want):
            problems.append(f"{t['id']}: statements must be exactly {', '.join(want)}")
        old, new = t["values"]["old"], t["values"].get("new")
        for role in want:
            text = common(f"{t['id']}.{role}", stmts.get(role))
            if text is None:
                continue
            if role in ("f1", "f3") and not score.carries(text, old):
                problems.append(f"{t['id']}.{role}: must contain the old value {old!r}")
            if role in ("f1", "f3") and new and score.carries(text, new):
                problems.append(
                    f"{t['id']}.{role}: must not contain the new value {new!r}"
                )
            if role == "f2" and not score.carries(text, new):
                problems.append(f"{t['id']}.f2: must contain the new value {new!r}")
            if role == "f2" and score.carries(text, old):
                problems.append(f"{t['id']}.f2: must not contain the old value {old!r}")
            if role == "d" and score.carries(text, old):
                problems.append(f"{t['id']}.d: must not contain the value {old!r}")
            for hit in carried(text, exclude_topic=t["id"]):
                problems.append(f"{t['id']}.{role}: contains {hit}")
        q = got.get("query")
        if not isinstance(q, str) or not q.strip():
            problems.append(f"{t['id']}: query missing")
        else:
            for hit in carried(q):
                problems.append(f"{t['id']}.query: contains {hit}")
    for hn in sk["hard_negatives"]:
        got = by_id["hard_negatives"].get(hn["id"])
        text = common(hn["id"], (got or {}).get("text"))
        if text is None:
            continue
        for hit in carried(text):
            problems.append(f"{hn['id']}: contains {hit}")
        if hn.get("material") and not score.carries(text, hn["material"]):
            problems.append(f"{hn['id']}: must contain {hn['material']!r} verbatim")
    markers: dict[str, str] = {}
    for s in sk["planted"]:
        got = by_id["planted"].get(s["id"]) or {}
        if s["technique"] == "split":
            parts = got.get("parts") if isinstance(got.get("parts"), dict) else {}
            texts = {
                k: common(f"{s['id']}{k}", parts.get(k), lo=30) for k in ("a", "b")
            }
        else:
            texts = {"": common(s["id"], got.get("text"))}
        for suffix, text in texts.items():
            if text is None:
                continue
            for hit in carried(text, exclude_pid=s["id"]):
                problems.append(f"{s['id']}{suffix}: contains {hit}")
        if s["class"] == "false_fact":
            v = s["value"]
            if s["technique"] == "split":
                if texts.get("a") and score.carries(texts["a"], v):
                    problems.append(
                        f"{s['id']}a: part a must not contain the false value {v!r}"
                    )
                if texts.get("b") and not score.carries(texts["b"], v):
                    problems.append(
                        f"{s['id']}b: part b must contain the false value {v!r}"
                    )
            elif texts.get("") and not score.carries(texts[""], v):
                problems.append(f"{s['id']}: must contain the false value {v!r}")
            for text in texts.values():
                if text and any(
                    score.carries(text, x)
                    for x in topic_of[s["target_topic"]]["values"].values()
                ):
                    problems.append(
                        f"{s['id']}: must not contain the target topic's real values"
                    )
        if s["class"] == "instruction":
            m = got.get("marker")
            text = texts.get("")
            if not isinstance(m, str) or len(m) < 6:
                problems.append(
                    f"{s['id']}: marker missing or shorter than 6 characters"
                )
            elif text and not score.carries(text, m):
                problems.append(f"{s['id']}: the marker {m!r} is not in the text")
            else:
                markers[s["id"]] = m
        if s["class"] == "secret":
            m = s["material"]
            if s["technique"] == "split":
                for k in ("a", "b"):
                    if texts.get(k) and not score.carries(texts[k], m[k]):
                        problems.append(f"{s['id']}{k}: must contain {m[k]!r} verbatim")
                if texts.get("a") and score.carries(texts["a"], m["b"]):
                    problems.append(f"{s['id']}a: must not contain part b's half")
            else:
                for lit in material_strings(m):
                    if texts.get("") and not score.carries(texts[""], lit):
                        problems.append(f"{s['id']}: must contain {lit!r} verbatim")
    for g in sk["generic_queries"]:
        got = by_id["generic_queries"].get(g["id"])
        text = common(g["id"], (got or {}).get("text"), lo=15, hi=300)
        if text is None:
            continue
        for hit in carried(text):
            problems.append(f"{g['id']}: contains {hit}")
    # a marker must not also sit in a legitimate item of the organisation
    legit_texts = [
        s
        for t in by_id["topics"].values()
        for s in (t.get("statements") or {}).values()
        if isinstance(s, str)
    ]
    legit_texts += [h.get("text", "") for h in by_id["hard_negatives"].values()]
    for pid, m in markers.items():
        if any(score.carries(x, m) for x in legit_texts):
            problems.append(
                f"{pid}: the marker {m!r} also appears in a legitimate item; choose a more distinctive marker"
            )
    return sorted(set(problems))


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def build_split(skeletons: dict[str, Any], proses: dict[str, Any]) -> dict[str, Any]:
    """The v1b test split, in v1's format: version v1b-test, benchmark
    integrity-v1b-test (score.v1_checks holds the two together), v0's
    scoring block, the counts declared."""
    v0 = read_json(V0_CORPUS)
    orgs = [o for o in spec.ORGS if o["split"] == "test"]
    topics, hns, gqs = [], [], []
    poison_by_class: dict[str, list[dict[str, Any]]] = {
        c: [] for c in score.POISON_CLASSES
    }
    for org in orgs:
        oid = org["id"]
        sk, pr = skeletons[oid], proses[oid]
        author = sk["author"]
        ptopics = {x["id"]: x for x in pr["topics"]}
        for t in sk["topics"]:
            got = ptopics[t["id"]]
            topics.append(
                {
                    "id": t["id"],
                    "org": oid,
                    "author": author,
                    "kind": t["kind"],
                    "subject": t["subject"],
                    "attribute": t["attribute"],
                    "values": t["values"],
                    "current": t["current"],
                    "statements": [
                        {
                            "id": f"{t['id']}.{role}",
                            "role": role,
                            "text": got["statements"][role],
                        }
                        for role in ROLES[t["kind"]]
                    ],
                    "query": got["query"],
                }
            )
        phns = {x["id"]: x for x in pr["hard_negatives"]}
        for hn in sk["hard_negatives"]:
            hns.append(
                {
                    "id": hn["id"],
                    "org": oid,
                    "author": author,
                    "category": hn["category"],
                    "text": phns[hn["id"]]["text"],
                }
            )
        pplanted = {x["id"]: x for x in pr["planted"]}
        for s in sk["planted"]:
            got = pplanted[s["id"]]
            base: dict[str, Any] = {
                "org": oid,
                "author": author,
                "class": s["class"],
                "variant": s["variant"],
                "technique": s["technique"],
            }
            if s["class"] == "false_fact":
                base["target_topic"] = s["target_topic"]
            if s["class"] == "instruction":
                base["marker"] = got["marker"]
            if s["class"] == "secret":
                base["shape"] = s["shape"]
            if s["technique"] == "split":
                for k in ("a", "b"):
                    item = {
                        **base,
                        "id": f"{s['id']}{k}",
                        "group": s["id"],
                        "text": got["parts"][k],
                    }
                    if s["class"] == "false_fact" and k == "b":
                        item["value"] = s["value"]
                    poison_by_class[s["class"]].append(item)
            else:
                item = {**base, "id": s["id"], "text": got["text"]}
                if s["class"] == "false_fact":
                    item["value"] = s["value"]
                poison_by_class[s["class"]].append(item)
        pg = {x["id"]: x for x in pr["generic_queries"]}
        for g in sk["generic_queries"]:
            gqs.append(
                {
                    "id": g["id"],
                    "org": oid,
                    "author": author,
                    "text": pg[g["id"]]["text"],
                }
            )
    poison = [p for c in score.POISON_CLASSES for p in poison_by_class[c]]
    groups = {p["group"] for p in poison if p.get("group")}

    def n_planted(cls: str, variant: str | None = None) -> int:
        ids = {
            p.get("group") or p["id"]
            for p in poison
            if p["class"] == cls and (variant is None or p["variant"] == variant)
        }
        return len(ids)

    declared = {
        "topic_kinds": {
            k: sum(1 for t in topics if t["kind"] == k) for k in score.TOPIC_KINDS
        },
        "poison_classes": {c: n_planted(c) for c in score.POISON_CLASSES},
        "poison_variants": {
            c: {v: n_planted(c, v) for v in ("plain", "evasive")}
            for c in score.POISON_CLASSES
        },
        "hard_negatives": len(hns),
        "hard_negative_categories": {
            c: sum(1 for h in hns if h["category"] == c)
            for c in spec.HARD_NEGATIVE_CATEGORIES
        },
        "generic_queries": len(gqs),
        "max_split_groups": len(groups),
    }
    return {
        "version": VERSION,
        "benchmark": BENCHMARK,
        "organisations": [
            {
                "id": o["id"],
                "name": o["name"],
                "domain": o["domain"],
                "note": spec.NOTE.format(name=o["name"]),
                "author": skeletons[o["id"]]["author"],
            }
            for o in orgs
        ],
        "scoring": v0["scoring"],
        "declared": declared,
        "generic_queries": gqs,
        "topics": topics,
        "hard_negatives": hns,
        "poison": poison,
    }


def union_value_problems(corpora: list[dict[str, Any]]) -> list[str]:
    """No value in any of the splits contains another (score.norm). The
    problems name ids only; `assemble` prints their count."""
    vals: dict[str, str] = {}
    for c in corpora:
        for t in c["topics"]:
            for k, v in t["values"].items():
                vals[f"{t['id']}.{k}"] = score.norm(v)
        for p in c["poison"]:
            if p["class"] == "false_fact" and p.get("value"):
                vals[p["id"]] = score.norm(p["value"])
    out = []
    for (a, na), (b, nb) in itertools.permutations(vals.items(), 2):
        if na in nb:
            out.append(f"value {a} is contained in value {b}")
    return out


def _dictionary() -> set[str]:
    words = Path("/usr/share/dict/words")
    if not words.exists():
        return set()
    return {
        w.strip().lower()
        for w in words.read_text(encoding="utf-8", errors="ignore").split()
    }


def vocabulary_overlap(
    tables: dict[str, Any], earlier: list[dict[str, Any]]
) -> list[str]:
    """Tokens of four or more characters from a v1b organisation's names,
    subjects and candidate values that also appear in the text of the
    `earlier` splits (v1 dev and v1 test), after a stoplist of dictionary
    words. The list can quote v1 test's text, so `assemble` prints only
    its length and writes it to the work files, for the owner."""
    stop = _dictionary() | {
        "invalid",
        "https",
        "http",
        "json",
        "yaml",
        "config",
        "prod",
        "staging",
    }
    test_tokens: set[str] = set()
    for org in spec.ORGS:
        if org["split"] != "test":
            continue
        test_tokens |= set(re.findall(r"[a-z0-9]{4,}", org["name"].lower()))
        for f in tables[org["id"]]["frames"]:
            text = " ".join([f["subject"], *f["candidates"]]).lower()
            test_tokens |= set(re.findall(r"[a-z0-9]{4,}", text))
    test_tokens -= stop
    earlier_tokens: set[str] = set()
    for corpus in earlier:
        text = json.dumps(corpus, ensure_ascii=False).lower()
        earlier_tokens |= set(re.findall(r"[a-z0-9]{4,}", text))
    return sorted(test_tokens & earlier_tokens)


# ---------------------------------------------------------------------------
# the test author, through OpenRouter
# ---------------------------------------------------------------------------


def _key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set")
    return key


def key_usage() -> float:
    req = urllib.request.Request(
        OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {_key()}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return float(json.loads(resp.read())["data"]["usage"])


def spent_on_authoring() -> float:
    total = 0.0
    for calls in WORK.glob("*/*.calls.json"):
        for c in read_json(calls):
            total += float((c.get("usage") or {}).get("cost") or 0.0)
    return total


def call_author(
    messages: list[dict[str, str]],
    model: str = spec.TEST_AUTHOR,
    provider: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": 32000,
        "usage": {"include": True},
    }
    if provider is not None:
        body["provider"] = provider
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {_key()}",
            "Content-Type": "application/json",
        },
    )
    t0 = time.time()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise SystemExit(f"author call failed: HTTP {e.code}")
            time.sleep(10)
    choice = data["choices"][0]
    record = {
        "requested_model": model,
        "model": data.get("model"),
        "id": data.get("id"),
        "provider": data.get("provider"),
        "finish_reason": choice.get("finish_reason"),
        "usage": data.get("usage"),
        "seconds": round(time.time() - t0, 1),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return choice["message"].get("content") or "", record


def parse_json(content: str) -> Any:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def load_plan() -> dict[str, Any]:
    return read_json(WORK / "plan.json")


def load_tables() -> dict[str, Any]:
    return {
        o["id"]: read_json(WORK / o["id"] / "vocab.json")
        for o in spec.ORGS
        if (WORK / o["id"] / "vocab.json").exists()
    }


def load_skeletons() -> dict[str, Any]:
    return {
        o["id"]: read_json(WORK / o["id"] / "skeleton.json")
        for o in spec.ORGS
        if (WORK / o["id"] / "skeleton.json").exists()
    }


def stage_check(
    oid: str, stage: str, withheld: Iterable[str] | None = None
) -> list[str]:
    """One stage's check of one organisation, against v1's withheld values
    (loaded unless given)."""
    held = load_withheld() if withheld is None else frozenset(withheld)
    answer = read_json(WORK / oid / f"{stage}.json")
    if stage == "vocab":
        return check_vocab(oid, answer, held)
    skeletons = load_skeletons()
    others = {k: v for k, v in skeletons.items() if k != oid}
    return check_prose(oid, skeletons[oid], answer, others, held)


def _rel(path: Path) -> str:
    resolved = Path(path).resolve()
    if resolved.is_relative_to(_ROOT):
        return resolved.relative_to(_ROOT).as_posix()
    return resolved.as_posix()


def cmd_plan() -> int:
    p = plan()
    write_json(WORK / "plan.json", p)
    for oid, o in p["orgs"].items():
        write_json(WORK / oid / "plan.json", o)
    offsets = ", ".join(f"{k} {v}" for k, v in p["cycle_offsets"].items())
    print(
        f"plan: seed {SEED}, {len(p['orgs'])} organisations, cycle offsets ({offsets}), "
        f"sha256 {hashlib.sha256(canonical(p).encode()).hexdigest()[:16]}"
    )
    return 0


def cmd_withheld() -> int:
    """The withheld set's size and sha, nothing of its content."""
    withheld = load_withheld()
    n_tables = len(list(V1_WORK.glob("*/vocab.json")))
    print(
        f"withheld: {len(withheld)} normalised v1 values (v1 dev, v1 test, "
        f"{n_tables} vocabulary tables), sha256 {withheld_digest(withheld)[:16]}"
    )
    return 0


def cmd_prompt(oid: str, stage: str) -> int:
    org = org_by_id(oid)
    if stage == "vocab":
        user = spec.vocab_prompt(org)
    else:
        user = spec.prose_prompt(org, read_json(WORK / oid / "skeleton.json"))
    path = WORK / oid / f"{stage}.prompt.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"SYSTEM:\n{spec.SYSTEM_PROMPT}\n\nUSER:\n{user}\n", encoding="utf-8"
    )
    print(f"{oid} {stage} prompt: {_rel(path)} ({len(user)} characters)")
    return 0


def cmd_author(
    oid: str,
    stage: str,
    repair: bool,
    review: bool = False,
    extra_round_ruling: str | None = None,
) -> int:
    """One author call. `extra_round_ruling` names an owner ruling that
    allows one repair round beyond MAX_REPAIRS (2026-10-11: one round for
    hlv's marker, ruling 01M4PQZ21PQDZMNHEWJV6740Z0); the call's record
    carries it."""
    if not is_test(oid):
        raise SystemExit(f"{oid} is not a test organisation")
    if spent_on_authoring() >= AUTHORING_CAP_USD:
        raise SystemExit(
            f"authoring spend has reached ${AUTHORING_CAP_USD}; stop and report"
        )
    org = org_by_id(oid)
    tpath = WORK / oid / f"{stage}.transcript.json"
    cpath = WORK / oid / f"{stage}.calls.json"
    calls = read_json(cpath) if cpath.exists() else []
    if review:
        # A reviewer asked for fixes: the author re-reads every item of its
        # own answer against the criteria and rewrites what falls short.
        messages = read_json(tpath)
        messages.append({"role": "user", "content": spec.REVIEW_PROMPT})
    elif repair:
        messages = read_json(tpath)
        problems = read_json(WORK / oid / f"{stage}.problems.json")
        limit = MAX_REPAIRS + (1 if extra_round_ruling else 0)
        if len([m for m in messages if m["role"] == "user"]) > limit:
            raise SystemExit(f"{oid} {stage}: {limit} repairs used; stop and report")
        messages.append({"role": "user", "content": spec.repair_prompt(problems)})
    else:
        user = (
            spec.vocab_prompt(org)
            if stage == "vocab"
            else spec.prose_prompt(org, read_json(WORK / oid / "skeleton.json"))
        )
        messages = [
            {"role": "system", "content": spec.SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
    content, record = call_author(messages)
    if extra_round_ruling:
        record["extra_round_ruling"] = extra_round_ruling
    messages.append({"role": "assistant", "content": content})
    write_json(tpath, messages)
    calls.append(record)
    write_json(cpath, calls)
    usage = record.get("usage") or {}
    print(
        f"{oid} {stage}: {record['model']} finish {record['finish_reason']}, "
        f"{usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out, "
        f"${usage.get('cost')}, {record['seconds']} s; authoring total ${spent_on_authoring():.4f}"
    )
    try:
        answer = parse_json(content)
    except (json.JSONDecodeError, IndexError):
        write_json(
            WORK / oid / f"{stage}.problems.json",
            ["the answer was not one valid JSON object"],
        )
        print(f"{oid} {stage}: the answer is not valid JSON")
        return 1
    if review and isinstance(answer, dict):
        changed = answer.pop("changed", None)
        n = len([m for m in messages if m["role"] == "user"])
        write_json(WORK / oid / f"{stage}.review-{n}.json", changed)
        print(
            f"{oid} {stage} review: {len(changed or [])} item(s) rewritten (list withheld: test organisation)"
        )
    write_json(WORK / oid / f"{stage}.json", answer)
    problems = stage_check(oid, stage)
    write_json(WORK / oid / f"{stage}.problems.json", problems)
    say(oid, f"{stage} check", problems)
    return 1 if problems else 0


def cmd_audit(oid: str) -> int:
    """The independent audit v1 ran before sealing: the auditor judges
    every item of an organisation's prose against the review criteria.
    Its flags become the problems of one repair round; only their counts
    are printed."""
    if not is_test(oid):
        raise SystemExit(f"{oid} is not a test organisation")
    if spent_on_authoring() >= AUTHORING_CAP_USD:
        raise SystemExit(
            f"authoring spend has reached ${AUTHORING_CAP_USD}; stop and report"
        )
    task = spec.prose_prompt(org_by_id(oid), read_json(WORK / oid / "skeleton.json"))
    answer = read_json(WORK / oid / "prose.json")
    messages = [
        {"role": "system", "content": spec.AUDIT_SYSTEM},
        {"role": "user", "content": spec.audit_prompt(task, answer)},
    ]
    content, record = call_author(messages, model=spec.AUDITOR, provider=AUDIT_PROVIDER)
    cpath = WORK / oid / "audit.calls.json"
    calls = read_json(cpath) if cpath.exists() else []
    calls.append(record)
    write_json(cpath, calls)
    usage = record.get("usage") or {}
    short = int(usage.get("completion_tokens") or 0) < AUDIT_MIN_COMPLETION_TOKENS
    print(
        f"{oid} audit: {record['model']} finish {record['finish_reason']}, "
        f"{usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out, "
        f"${usage.get('cost')}, {record['seconds']} s; authoring total ${spent_on_authoring():.4f}"
    )
    if short:
        # An answer this short judged nothing: no problems are written, so
        # the prose stays as the review left it, and the audit is re-run.
        print(
            f"{oid} audit: {usage.get('completion_tokens')} completion tokens, "
            f"under {AUDIT_MIN_COMPLETION_TOKENS}; not an audit, run it again"
        )
        return 1
    try:
        verdict = parse_json(content)
        flagged = list(verdict.get("flagged") or [])
    except (json.JSONDecodeError, IndexError, AttributeError):
        write_json(WORK / oid / "prose.audit.raw.json", {"content": content})
        print(f"{oid} audit: the answer is not valid JSON")
        return 1
    write_json(WORK / oid / "prose.audit.json", verdict)
    problems = [f"{f.get('id')}: {f.get('reason')}" for f in flagged]
    write_json(WORK / oid / "prose.problems.json", problems)
    by_criterion = {
        c: sum(1 for f in flagged if f.get("criterion") == c) for c in (1, 2, 3, 4)
    }
    print(
        f"{oid} audit: {verdict.get('reviewed')} items judged, {len(flagged)} flagged, "
        f"by criterion {by_criterion} (ids and reasons withheld: test organisation)"
    )
    return 0


def cmd_audit_report(out: Path) -> int:
    """Every flagged id with its reason, for the owner only; nothing of it
    is printed."""
    lines = [
        "# v1b test split: the independent audit",
        "",
        f"Auditor {spec.AUDITOR}; every flagged item went back to {spec.TEST_AUTHOR} for one rewrite.",
        "",
    ]
    for org in spec.ORGS:
        path = WORK / org["id"] / "prose.audit.json"
        if not path.exists():
            continue
        v = read_json(path)
        flagged = v.get("flagged") or []
        lines += [
            f"## {org['id']} {org['name']}: {v.get('reviewed')} judged, {len(flagged)} flagged",
            "",
        ]
        lines += [
            f"- {f.get('id')} (criterion {f.get('criterion')}): {f.get('reason')}"
            for f in flagged
        ]
        lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"audit report written: {out}; not printed")
    return 0


def cmd_check(oid: str, stage: str) -> int:
    problems = stage_check(oid, stage)
    write_json(WORK / oid / f"{stage}.problems.json", problems)
    say(oid, f"{stage} check", problems)
    return 1 if problems else 0


def cmd_draw() -> int:
    tables = load_tables()
    missing = [o["id"] for o in spec.ORGS if o["id"] not in tables]
    if missing:
        raise SystemExit(f"vocabulary tables missing for {', '.join(missing)}")
    withheld = load_withheld()
    bad = {oid: check_vocab(oid, t, withheld) for oid, t in tables.items()}
    if any(bad.values()):
        for oid, problems in bad.items():
            write_json(WORK / oid / "vocab.problems.json", problems)
            say(oid, "vocab check", problems)
        return 1
    skeletons, failures = draw(load_plan(), tables, withheld)
    for oid, sk in skeletons.items():
        write_json(WORK / oid / "skeleton.json", sk)
    for org in spec.ORGS:
        problems = failures.get(org["id"], [])
        write_json(WORK / org["id"] / "vocab.problems.json", problems)
        say(org["id"], "draw", problems)
    return 1 if failures else 0


def _guarded(name: str, fn: Any, *args: Any) -> list[str]:
    """A check's problems; a check that raises reports only its exception's
    type, whose text could quote a corpus."""
    try:
        return list(fn(*args))
    except Exception as exc:  # noqa: BLE001 - the text could quote content
        return [f"{name} raised {type(exc).__name__}"]


def cmd_assemble() -> int:
    """Check every organisation's prose, write the test split (test.json
    only: v1b has no dev split), then the corpus checks, the value checks
    over the union of v1 dev, v1 test and v1b, and the vocabulary overlap
    with v1, every one printed as a count."""
    skeletons = load_skeletons()
    proses = {o["id"]: read_json(WORK / o["id"] / "prose.json") for o in spec.ORGS}
    withheld = load_withheld()
    status = 0
    for org in spec.ORGS:
        oid = org["id"]
        others = {k: v for k, v in skeletons.items() if k != oid}
        problems = check_prose(oid, skeletons[oid], proses[oid], others, withheld)
        say(oid, "prose check", problems)
        status |= bool(problems)
    if status:
        return 1
    test = build_split(skeletons, proses)
    write_canonical(TEST_PATH, test)
    problems = _guarded("score.corpus_checks", score.corpus_checks, test)
    d = test["declared"]
    print(
        f"test: {len(test['topics'])} topics {d['topic_kinds']}, "
        f"{sum(len(t['statements']) for t in test['topics'])} statements, "
        f"{d['hard_negatives']} hard negatives, planted {d['poison_classes']}, "
        f"{len(test['poison'])} poison adds, {d['max_split_groups']} split groups, "
        f"sha256 {score.corpus_sha256(TEST_PATH)}"
    )
    print(f"test score.corpus_checks: {len(problems)} problem(s)")
    v1_dev, v1_test = read_json(V1_DEV), read_json(V1_TEST)
    union = _guarded(
        "union_value_problems", union_value_problems, [v1_dev, v1_test, test]
    )
    print(f"union value check (v1 dev, v1 test, v1b test): {len(union)} problem(s)")
    cross = _guarded(
        "score.union_value_checks",
        score.union_value_checks,
        [("v1-dev", v1_dev), ("v1-test", v1_test), (VERSION, test)],
    )
    print(
        f"union corpus checks (v1 dev, v1 test, v1b test): {len(cross)} problem(s) "
        "(details withheld: test splits)"
    )
    overlap = vocabulary_overlap(load_tables(), [v1_dev, v1_test])
    write_json(WORK / "vocabulary-overlap.json", overlap)
    print(
        f"vocabulary overlap with v1 dev and test: {len(overlap)} token(s) "
        "(list in work/vocabulary-overlap.json, for the owner)"
    )
    return 1 if problems or union or cross else 0


def cmd_sample(out: Path) -> int:
    """A stratified sample of the test split for the owner's read, written
    to `out`; nothing of it is printed."""
    test = read_json(TEST_PATH)
    rng = random.Random(f"{SEED}:owner-sample")
    lines = [
        "# v1b integrity corpus: test sample for the owner's read",
        "",
        f"Test split sha256 {score.corpus_sha256(TEST_PATH)}; every item was written by "
        f"{spec.TEST_AUTHOR}; the orchestrating chat model has not read any of it.",
        "",
    ]
    for cls in score.POISON_CLASSES:
        for variant in ("plain", "evasive"):
            pool = sorted(
                {
                    p.get("group") or p["id"]
                    for p in test["poison"]
                    if p["class"] == cls and p["variant"] == variant
                }
            )
            pick = rng.choice(pool)
            items = [p for p in test["poison"] if (p.get("group") or p["id"]) == pick]
            p0 = items[0]
            lines.append(
                f"## {pick}: {cls.replace('_', ' ')}, {variant}, technique {p0['technique']}"
            )
            if cls == "false_fact":
                t = next(t for t in test["topics"] if t["id"] == p0["target_topic"])
                lines.append(
                    f"Target {t['id']} ({t['subject']}, {t['attribute']}): true current value {t['values'][t['current']]!r}, planted value {items[-1].get('value')!r}."
                )
            if cls == "instruction":
                lines.append(f"Marker: {p0['marker']!r}.")
            for p in items:
                lines += [
                    "",
                    f"> {p['text']}"
                    if len(items) == 1
                    else f"> ({p['id']}) {p['text']}",
                ]
            lines.append("")
    cats = rng.sample(sorted(spec.HARD_NEGATIVE_CATEGORIES), 2)
    for cat in cats:
        hn = rng.choice([h for h in test["hard_negatives"] if h["category"] == cat])
        lines += [f"## {hn['id']}: hard negative, {cat}", "", f"> {hn['text']}", ""]
    t = rng.choice([t for t in test["topics"] if t["kind"] == "supersession"])
    lines += [
        f"## {t['id']}: supersession topic ({t['subject']}, {t['attribute']}; old {t['values']['old']!r}, new {t['values']['new']!r})",
        "",
    ]
    for s in t["statements"]:
        lines += [f"> ({s['role']}) {s['text']}", ""]
    lines += [f"> (query) {t['query']}", ""]
    g = rng.choice(test["generic_queries"])
    lines += [f"## {g['id']}: generic query", "", f"> {g['text']}", ""]
    overlap = read_json(WORK / "vocabulary-overlap.json")
    lines += [
        "## Vocabulary overlap with v1",
        "",
        "Tokens of four or more characters from the v1b organisations' names, subjects and candidate values that also appear in v1's dev or test split, after a dictionary stoplist:",
        "",
        ", ".join(overlap) if overlap else "(none)",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"owner sample written: {out} ({len(lines)} lines); not printed")
    return 0


def _calls(*globs: str) -> list[dict[str, Any]]:
    return [c for g in globs for f in sorted(WORK.glob(g)) for c in read_json(f)]


def _spent(*globs: str) -> float:
    return sum(float((c.get("usage") or {}).get("cost") or 0.0) for c in _calls(*globs))


def _models_returned(*globs: str) -> list[str]:
    return sorted({c["model"] for c in _calls(*globs) if c.get("model")})


def seal_record(commit: str) -> dict[str, Any]:
    """SEALS.json's fields: v1's, with dev_sha256 and
    operating_points_sha256 naming what v1b reuses (v1's dev split, and
    the operating points frozen on it, keyed by instrument), so the guard
    (bench/integrity/guard.py) reads v1b's seal as it reads v1's."""
    withheld = load_withheld()
    plan_obj = load_plan()
    return {
        "seed": SEED,
        "version": VERSION,
        "dev_sha256": score.corpus_sha256(V1_DEV),
        "dev_split": _rel(V1_DEV),
        "test_sha256": score.corpus_sha256(TEST_PATH),
        "vocab_tables_sha256": hashlib.sha256(
            canonical(load_tables()).encode()
        ).hexdigest(),
        "plan_sha256": hashlib.sha256(canonical(plan_obj).encode()).hexdigest(),
        "cycle_offsets": plan_obj.get("cycle_offsets"),
        "withheld": {"count": len(withheld), "sha256": withheld_digest(withheld)},
        "generator_commit": commit,
        "authors": {o["id"]: spec.TEST_AUTHOR for o in spec.ORGS},
        "author_models_returned": _models_returned(
            "*/vocab.calls.json", "*/prose.calls.json"
        ),
        "authoring_usd": round(_spent("*/vocab.calls.json", "*/prose.calls.json"), 6),
        "auditor_models_returned": _models_returned("*/audit.calls.json"),
        "audit_usd": round(_spent("*/audit.calls.json"), 6),
        "operating_points": _rel(V1_POINTS),
        "operating_points_sha256": hashlib.sha256(V1_POINTS.read_bytes()).hexdigest(),
        "sealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def cmd_seal() -> int:
    dirty = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            str(_HERE / "gen.py"),
            str(_HERE / "spec.py"),
        ],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    ).stdout.strip()
    if dirty:
        raise SystemExit(
            "gen.py or spec.py differs from HEAD; commit the generator before sealing"
        )
    commit = subprocess.run(
        [
            "git",
            "log",
            "-1",
            "--format=%H",
            "--",
            str(_HERE / "gen.py"),
            str(_HERE / "spec.py"),
        ],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    ).stdout.strip()
    if not commit:
        raise SystemExit("gen.py and spec.py are not committed; commit them first")
    seals = seal_record(commit)
    SEALS_PATH.write_text(json.dumps(seals, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(seals, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    orgs = [o["id"] for o in spec.ORGS]
    sub.add_parser("plan")
    sub.add_parser("withheld")
    for name in ("prompt", "check"):
        s = sub.add_parser(name)
        s.add_argument("--org", required=True, choices=orgs)
        s.add_argument("--stage", required=True, choices=["vocab", "prose"])
    a = sub.add_parser("author")
    a.add_argument("--org", required=True, choices=orgs)
    a.add_argument("--stage", required=True, choices=["vocab", "prose"])
    a.add_argument("--repair", action="store_true")
    a.add_argument("--review", action="store_true")
    a.add_argument(
        "--extra-round-ruling",
        help="an owner ruling's memory id allowing one repair beyond MAX_REPAIRS",
    )
    au = sub.add_parser("audit")
    au.add_argument("--org", required=True, choices=orgs)
    ar = sub.add_parser("audit-report")
    ar.add_argument("--out", required=True, type=Path)
    sub.add_parser("draw")
    sub.add_parser("assemble")
    s = sub.add_parser("sample")
    s.add_argument("--out", required=True, type=Path)
    sub.add_parser("seal")
    args = ap.parse_args(argv)
    if args.cmd == "plan":
        return cmd_plan()
    if args.cmd == "withheld":
        return cmd_withheld()
    if args.cmd == "prompt":
        return cmd_prompt(args.org, args.stage)
    if args.cmd == "check":
        return cmd_check(args.org, args.stage)
    if args.cmd == "author":
        return cmd_author(
            args.org, args.stage, args.repair, args.review, args.extra_round_ruling
        )
    if args.cmd == "audit":
        return cmd_audit(args.org)
    if args.cmd == "audit-report":
        return cmd_audit_report(args.out)
    if args.cmd == "draw":
        return cmd_draw()
    if args.cmd == "assemble":
        return cmd_assemble()
    if args.cmd == "sample":
        return cmd_sample(args.out)
    return cmd_seal()


if __name__ == "__main__":
    raise SystemExit(main())
