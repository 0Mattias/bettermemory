"""A dense embedding trained from scratch, by this file, from committed text.

P1a asked whether store-derived co-occurrence (raw PPMI) could replace
the committed expansion tables and was killed at its gate: 0.1253
precision against the tables' 0.2743, with no cell of a 36-point grid
reaching parity. The signal was there — PPMI found 150+ gold terms the
tables miss — and it arrived buried under 10-65 terms per probe.

P1e is the obvious follow-up and the one the owner's 2026-08-11 doctrine
update makes admissible: **a dense factorization of the same statistic,
trained rather than counted.** Neural weights are WaC-legal when the
model is built from scratch here; pretrained third-party weights are
not. So this module trains GloVe — the co-occurrence-factorizing member
of the word2vec family, chosen because its cost scales with the number
of *distinct* co-occurring pairs rather than with the token stream,
which is what makes a pure-Python trainer finish at all.

**Nothing here is a model file. It is a derivation:**

- every training input is committed repository text, enumerated by
  `SOURCES` with its licence stated next to it. The one exception,
  `lme`, is labelled as an uncommitted diagnostic wherever it appears;
- no network at derivation or at use; no third-party model code, and no
  third-party numerics either — the dependency tree has carried no numpy
  since 4.0, so the linear algebra below is written out;
- fixed seed, sorted iteration order, and a shuffle drawn from a seeded
  `random.Random`, so two runs produce byte-identical vectors. `--twice`
  checks that rather than asserting it;
- the artifact carries a provenance block naming every source file, its
  sha256, the parameters and the trainer's own sha256, plus one
  `corpus_manifest_sha256` over the lot. That digest earns its place:
  the `repo` corpus IS the working tree, so a model trained from it is
  a function of the checkout, and `--twice` uses the manifest to tell
  "someone edited a docstring mid-run" apart from "the optimiser is not
  deterministic". Only the second would be a defect.

The vectors are NOT committed. They are a derived intermediate of a
committed deterministic script over committed inputs, which is the
reproducibility bar the third-instrument note states; committing a
multi-megabyte float dump would break the repository's 500 kB
added-file cap to store something the script reproduces exactly.

    .venv/bin/python bench/embed_train.py --corpus store --out /tmp/store.json

Consumed by `bench/embed_census.py`, which is where any claim about
whether these vectors are USEFUL lives. This file makes vectors; it
makes no retrieval claim and touches no engine path.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import math
import random
import re
import sys
import time
import tokenize as py_tokenize
from pathlib import Path
from typing import Any, Iterable, NamedTuple

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bettermemory.search import _expand_kebab, tokenize  # noqa: E402

# --------------------------------------------------------------------------
# Training parameters. Declared here, stamped into every artifact, and
# swept nowhere — this file has no outcome to tune against. The census
# sweeps the EMISSION rule (how many neighbours, at what similarity),
# which is the parameter a mechanism would actually carry.
# --------------------------------------------------------------------------

WINDOW = 5
MIN_COUNT = 5
MAX_VOCAB = 20000
MIN_COOC = 1.0
DIM = 64
EPOCHS = 15
LR = 0.05
XMAX = 50.0
ALPHA = 0.75
SEED = 20260811


class Source(NamedTuple):
    """One licence-stamped input to a training corpus.

    `kind` selects the extractor. `licence` is recorded in the artifact
    and is the reason each entry is admissible, not decoration: an
    unlicensed input would make the derived vectors unshippable however
    good they measured.
    """

    kind: str  # "jsonl-body" | "markdown" | "python-prose"
    path: str  # repository-relative file or directory
    glob: str  # "" for a single file
    licence: str


# Every corpus is assembled from files this repository already tracks,
# so staging costs zero committed bytes and inherits the repository's own
# MIT grant.
#
# TWO exclusions, both load-bearing, both for the same reason — a model
# must not be trained on text that discusses the instrument it is about
# to be scored against:
#
# - `bench/` prose. bench/retrieval/README.md spells out this
#   instrument's paraphrase pairs in so many words ("'toggles' vs
#   'feature flags', 'creds' vs 'credentials'"). A model trained on it
#   would be handed the answer key and the census would measure nothing.
# - `tests/`. Less obvious and found the same day: this census's own
#   test module cites 'split'/'splitting' and 'credential' as
#   morphology and clipping examples, which is the same answer key one
#   directory over. Excluding the tree also makes the corpus stable
#   against our own test-writing — otherwise every commit that adds a
#   test changes the training corpus and no artifact reproduces.
_STORE = (
    Source("jsonl-body", "bench/retrieval/corpus.jsonl", "", "MIT (this repository)"),
)
_REPO = (
    Source("markdown", "docs", "**/*.md", "MIT (this repository)"),
    Source("markdown", "README.md", "", "MIT (this repository)"),
    Source("markdown", "CHANGELOG.md", "", "MIT (this repository)"),
    Source("markdown", "CONTRIBUTING.md", "", "MIT (this repository)"),
    Source("markdown", "plugin", "**/*.md", "MIT (this repository)"),
    Source("python-prose", "src", "**/*.py", "MIT (this repository)"),
)
SOURCES: dict[str, tuple[Source, ...]] = {
    "store": _STORE,
    "repo": _REPO,
    "repo+store": _REPO + _STORE,
}

# The conversational arm, and the one corpus here that is NOT committed.
# `bench/longmemeval/data/` is gitignored, so an artifact trained on it
# is reproducible only for someone holding the same download — the same
# limitation `bench/df_census.py` and `bench/store_census.py` already
# carry, and it is stated in the census record rather than implied.
# It exists because the register question has no committed answer: this
# repository's own prose is technical, the gold set's corpus is
# technical, and the failure the campaign is chasing is conversational.
#
# Training instances are drawn from a slice DISJOINT from the slice the
# census scores, so this measures a model transferring to an unseen
# store of the same register rather than one trained on its own answers.
LME_TRAIN_SLICE = (20, 60)
LME_MAX_TOKENS = 900_000

CORPORA: tuple[str, ...] = ("store", "repo", "repo+store", "lme")

_PARA_SPLIT = re.compile(r"\n\s*\n")


def load_bench_module(name: str, path: Path) -> Any:
    """Import a bench runner under an explicit module name.

    `bench/retrieval/run.py` and `bench/longmemeval/run.py` are both
    called `run`, so a `sys.path` insert can only ever reach one of them
    per process. `bench/store_census.py` solves it this way and the
    census below needs BOTH runners in one run.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Reading committed text
# --------------------------------------------------------------------------


def _paragraphs(text: str) -> list[str]:
    """Split on blank lines and unwrap.

    This repository hard-wraps its prose at ~72 columns, so treating a
    LINE as the co-occurrence unit would cut most windows in half and
    measure the wrap width as much as the language. A paragraph is the
    smallest unit that survives that.
    """
    out = []
    for block in _PARA_SPLIT.split(text):
        joined = " ".join(block.split())
        if joined:
            out.append(joined)
    return out


def _python_prose(text: str) -> list[str]:
    """Docstrings and comments — the English inside a Python file.

    Code tokens are deliberately dropped. `store.write(content=text)` is
    not a sentence, and feeding identifiers into a window model buys
    co-occurrences that describe the call graph rather than the
    vocabulary. Comments are read through the stdlib tokenizer rather
    than by regex so a `#` inside a string literal is not mistaken for
    one.
    """
    chunks: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            doc = ast.get_docstring(node, clean=True)
            if doc:
                chunks.append(doc)
    try:
        for tok in py_tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == py_tokenize.COMMENT:
                chunks.append(tok.string.lstrip("#").strip())
    except (py_tokenize.TokenError, IndentationError):
        pass
    return chunks


def _files(source: Source) -> list[Path]:
    """Every file a source names, in sorted order.

    Sorted because iteration order is part of the artifact: the
    co-occurrence accumulator is float-valued, and float addition is not
    associative, so a different file order is a different model.
    """
    base = _ROOT / source.path
    if not source.glob:
        return [base] if base.is_file() else []
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob(source.glob) if p.is_file())


def read_units(sources: Iterable[Source]) -> tuple[list[str], list[dict[str, Any]]]:
    """Text units plus the per-file provenance rows that identify them."""
    units: list[str] = []
    provenance: list[dict[str, Any]] = []
    for source in sources:
        for path in _files(source):
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            if source.kind == "jsonl-body":
                chunks = [
                    json.loads(line)["body"]
                    for line in text.splitlines()
                    if line.strip()
                ]
            elif source.kind == "python-prose":
                chunks = _python_prose(text)
            else:
                chunks = [text]
            before = len(units)
            for chunk in chunks:
                units.extend(_paragraphs(chunk))
            provenance.append(
                {
                    "path": str(path.relative_to(_ROOT)),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "kind": source.kind,
                    "licence": source.licence,
                    "units": len(units) - before,
                }
            )
    return units, provenance


def read_lme_units() -> tuple[list[str], list[dict[str, Any]]]:
    """Haystack rounds from LongMemEval instances outside the scored slice.

    Rounds are paired exactly as `bench/longmemeval/run.py` pairs them
    when it writes a question store, so the training text is the text
    the engine would have indexed — not the raw transcript.
    """
    lr = load_bench_module("embed_train_lme", _HERE / "longmemeval" / "run.py")
    path = lr.DEFAULT_CORPUS
    if not path.exists():
        raise SystemExit(
            f"{path} is absent — the LongMemEval corpus is not committed "
            "(bench/longmemeval/data is gitignored); see that directory's README."
        )
    corpus = json.loads(path.read_text(encoding="utf-8"))
    lo, hi = LME_TRAIN_SLICE
    units: list[str] = []
    used = 0
    approx = 0
    for inst in corpus[lo:hi]:
        used += 1
        for session in inst["haystack_sessions"]:
            for body in lr.rounds_of(session):
                units.extend(_paragraphs(body))
        approx = sum(len(u.split()) for u in units)
        if approx >= LME_MAX_TOKENS:
            break
    return units, [
        {
            "path": str(path.relative_to(_ROOT)),
            "sha256": lr.corpus_fingerprint(path),
            "bytes": path.stat().st_size,
            "kind": "longmemeval-haystack",
            "licence": (
                "NOT COMMITTED — gitignored download, no redistribution grant; "
                "this arm is a diagnostic, not a shippable derivation"
            ),
            "units": len(units),
            "instances": used,
            "instance_slice": list(LME_TRAIN_SLICE),
        }
    ]


def corpus_manifest(provenance: list[dict[str, Any]]) -> str:
    """One digest naming every byte that trained the model.

    Two corpora here read fixed files, but `repo` is the WORKING TREE —
    so a model trained from it is a function of the checkout, and two
    runs across an edit are honestly two different models. This digest
    is what lets `--twice` say which of those happened instead of
    blaming the optimiser.
    """
    lines = "\n".join(
        f"{row['path']} {row['sha256']}"
        for row in sorted(provenance, key=lambda r: r["path"])
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def token_streams(units: list[str]) -> list[list[str]]:
    """Each unit through the ENGINE's tokenizer, index-side.

    `_expand_kebab(tokenize(...))` is exactly `_memory_tokens().body`, so
    the vocabulary these vectors are indexed by is the vocabulary the
    BM25 legs score against. A model trained in a different token space
    could not emit a term the index can match, and the census would be
    measuring a spelling mismatch rather than a mechanism.
    """
    return [_expand_kebab(tokenize(unit)) for unit in units]


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------


def build_vocab(streams: list[list[str]]) -> tuple[list[str], dict[str, int]]:
    """Terms at or above `MIN_COUNT`, capped at `MAX_VOCAB`.

    Ordered by descending count with the term string breaking ties, so
    the index assignment is a function of the corpus alone.
    """
    counts: dict[str, int] = {}
    for stream in streams:
        for tok in stream:
            counts[tok] = counts.get(tok, 0) + 1
    kept = [t for t, c in counts.items() if c >= MIN_COUNT]
    kept.sort(key=lambda t: (-counts[t], t))
    kept = kept[:MAX_VOCAB]
    return kept, {t: i for i, t in enumerate(kept)}


def cooccurrence(
    streams: list[list[str]], index: dict[str, int], *, window: int = WINDOW
) -> dict[tuple[int, int], float]:
    """Distance-weighted co-occurrence counts, GloVe's 1/d weighting.

    Both directions are accumulated, so `X[i][j] == X[j][i]` and the two
    vector sets the trainer learns are symmetric in expectation. Terms
    outside the vocabulary are skipped WITHOUT closing the window over
    them — dropping a rare word must not make its neighbours adjacent,
    or the model learns co-occurrences the text does not contain.
    """
    counts: dict[tuple[int, int], float] = {}
    for stream in streams:
        ids = [index.get(tok, -1) for tok in stream]
        n = len(ids)
        for pos in range(n):
            centre = ids[pos]
            if centre < 0:
                continue
            hi = min(n, pos + window + 1)
            for other in range(pos + 1, hi):
                ctx = ids[other]
                if ctx < 0:
                    continue
                weight = 1.0 / (other - pos)
                key = (centre, ctx)
                counts[key] = counts.get(key, 0.0) + weight
                key = (ctx, centre)
                counts[key] = counts.get(key, 0.0) + weight
    return counts


# --------------------------------------------------------------------------
# Training
# --------------------------------------------------------------------------


def weight(x: float) -> float:
    """GloVe's `f(x)`: damp rare pairs, saturate at `XMAX`.

    The only place a co-occurrence count enters the objective other than
    through its logarithm, which is why it is named rather than inlined
    — a census whose weighting differed from GloVe's would be reporting
    on a different estimator.
    """
    return 1.0 if x >= XMAX else (x / XMAX) ** ALPHA


def train(
    counts: dict[tuple[int, int], float],
    vocab_size: int,
    *,
    dim: int = DIM,
    epochs: int = EPOCHS,
    lr: float = LR,
    seed: int = SEED,
    log: bool = False,
) -> tuple[list[list[float]], list[float]]:
    """GloVe by AdaGrad. Returns (word + context vectors, per-epoch loss).

    The objective, written out because no library supplies it here:

        J = sum_ij f(X_ij) (w_i . c_j + b_i + d_j - log X_ij)^2
        f(x) = (x / XMAX)^ALPHA  if x < XMAX else 1

    Determinism, which is the property this function exists to have:
    the cell list is sorted before anything touches it; the per-epoch
    shuffle is drawn from a `random.Random` seeded once; initialisation
    draws from the same generator in index order. Float addition is not
    associative, so all three orderings are part of the result.
    """
    rng = random.Random(seed)
    scale = 0.5 / dim
    w = [[(rng.random() - 0.5) * scale for _ in range(dim)] for _ in range(vocab_size)]
    c = [[(rng.random() - 0.5) * scale for _ in range(dim)] for _ in range(vocab_size)]
    bw = [0.0] * vocab_size
    bc = [0.0] * vocab_size
    gw = [[1.0] * dim for _ in range(vocab_size)]
    gc = [[1.0] * dim for _ in range(vocab_size)]
    gbw = [1.0] * vocab_size
    gbc = [1.0] * vocab_size

    cells = sorted(counts.items())
    prepared = [(i, j, weight(x), math.log(x)) for (i, j), x in cells]

    losses: list[float] = []
    for epoch in range(epochs):
        rng.shuffle(prepared)
        total = 0.0
        for i, j, fx, logx in prepared:
            wi = w[i]
            cj = c[j]
            dot = 0.0
            for d in range(dim):
                dot += wi[d] * cj[d]
            diff = dot + bw[i] + bc[j] - logx
            total += fx * diff * diff
            grad = 2.0 * fx * diff
            gwi = gw[i]
            gcj = gc[j]
            for d in range(dim):
                gi = grad * cj[d]
                gj = grad * wi[d]
                wi[d] -= lr * gi / math.sqrt(gwi[d])
                cj[d] -= lr * gj / math.sqrt(gcj[d])
                gwi[d] += gi * gi
                gcj[d] += gj * gj
            bw[i] -= lr * grad / math.sqrt(gbw[i])
            bc[j] -= lr * grad / math.sqrt(gbc[j])
            gbw[i] += grad * grad
            gbc[j] += grad * grad
        loss = total / (2.0 * len(prepared)) if prepared else 0.0
        losses.append(loss)
        if log:
            print(f"  epoch {epoch + 1}/{epochs}  loss {loss:.6f}", file=sys.stderr)

    # w + c is GloVe's own recommendation: the two sets are symmetric
    # under the objective, and summing them averages away the arbitrary
    # split between a term's word and context roles.
    return [[w[i][d] + c[i][d] for d in range(dim)] for i in range(vocab_size)], losses


def unit_normalise(vectors: list[list[float]]) -> list[list[float]]:
    """L2-normalise so a cosine is a dot product.

    A zero vector stays zero rather than becoming NaN; the census treats
    it as "no usable vector" and reports it under coverage.
    """
    out: list[list[float]] = []
    for vec in vectors:
        norm = math.sqrt(sum(v * v for v in vec))
        out.append([v / norm for v in vec] if norm > 0.0 else [0.0] * len(vec))
    return out


def mean_centre(vectors: list[list[float]]) -> list[list[float]]:
    """Subtract the corpus mean vector.

    Trained word vectors share a large common component that carries
    frequency rather than meaning, and it dominates a raw cosine — the
    first neighbour list this trainer produced put 'just', 'good' and
    'lower' at 1.00 against 'rollback'. Removing the mean is the
    standard remedy and it is applied HERE rather than baked into the
    artifact, so the census can sweep it as one of the two readings it
    reports instead of inheriting it as a hidden choice.
    """
    if not vectors:
        return []
    dim = len(vectors[0])
    n = float(len(vectors))
    mean = [sum(v[d] for v in vectors) / n for d in range(dim)]
    return [[v[d] - mean[d] for d in range(dim)] for v in vectors]


# --------------------------------------------------------------------------
# Artifact
# --------------------------------------------------------------------------

_ROUND = 8


def build(corpus: str, *, dim: int, epochs: int, log: bool = False) -> dict[str, Any]:
    """Train one corpus end to end and return the artifact payload."""
    if corpus not in CORPORA:
        raise SystemExit(f"unknown corpus {corpus!r}; choose from {sorted(CORPORA)}")
    started = time.time()
    if corpus == "lme":
        units, provenance = read_lme_units()
    else:
        units, provenance = read_units(SOURCES[corpus])
    if not provenance:
        raise SystemExit(
            f"corpus {corpus!r} matched no files — nothing is staged for it"
        )
    streams = token_streams(units)
    vocab, index = build_vocab(streams)
    counts = cooccurrence(streams, index)
    counts = {k: v for k, v in counts.items() if v >= MIN_COOC}
    if log:
        print(
            f"  units {len(units)}  tokens {sum(len(s) for s in streams)}  "
            f"vocab {len(vocab)}  cells {len(counts)}",
            file=sys.stderr,
        )
    vectors, losses = train(counts, len(vocab), dim=dim, epochs=epochs, log=log)
    if log:
        print(f"  trained in {time.time() - started:.1f}s", file=sys.stderr)
    # Deliberately no wall-clock field in the payload: the artifact has
    # to be byte-identical across runs for the determinism check to mean
    # anything, and a duration is the one thing that never repeats.
    # Vectors are stored RAW (w + c, unnormalised, uncentred) so every
    # downstream transform is the census's declared choice, not a
    # decision hidden inside a float dump.
    return {
        "kind": "bettermemory-p1e-vectors",
        "corpus": corpus,
        "trainer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "parameters": {
            "window": WINDOW,
            "min_count": MIN_COUNT,
            "max_vocab": MAX_VOCAB,
            "min_cooc": MIN_COOC,
            "dim": dim,
            "epochs": epochs,
            "lr": LR,
            "xmax": XMAX,
            "alpha": ALPHA,
            "seed": SEED,
            "round": _ROUND,
        },
        "corpus_stats": {
            "files": len(provenance),
            "units": len(units),
            "tokens": sum(len(s) for s in streams),
            "vocab": len(vocab),
            "cooccurrence_cells": len(counts),
        },
        "final_loss": round(losses[-1], 6) if losses else 0.0,
        "losses": [round(x, 6) for x in losses],
        "corpus_manifest_sha256": corpus_manifest(provenance),
        "sources": provenance,
        "vocab": vocab,
        "vectors": [[round(v, _ROUND) for v in vec] for vec in vectors],
    }


def load(path: Path) -> tuple[list[str], dict[str, list[float]], dict[str, Any]]:
    """Read an artifact back as (vocab, term -> vector, metadata)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    vocab = payload["vocab"]
    vectors = {t: payload["vectors"][i] for i, t in enumerate(vocab)}
    meta = {k: v for k, v in payload.items() if k not in ("vocab", "vectors")}
    return vocab, vectors, meta


def main() -> int:
    p = argparse.ArgumentParser(
        description="Train a from-scratch GloVe embedding from committed text."
    )
    p.add_argument("--corpus", required=True, choices=sorted(CORPORA))
    p.add_argument("--out", required=True, metavar="PATH")
    p.add_argument("--dim", type=int, default=DIM)
    p.add_argument("--epochs", type=int, default=EPOCHS)
    p.add_argument("--quiet", action="store_true")
    p.add_argument(
        "--twice",
        action="store_true",
        help=(
            "Train the same corpus a second time and compare the two "
            "payloads byte for byte. The determinism claim is checkable by "
            "one command rather than by trusting a transcript; a mismatch "
            "exits non-zero."
        ),
    )
    args = p.parse_args()

    payload = build(args.corpus, dim=args.dim, epochs=args.epochs, log=not args.quiet)
    if args.twice:
        again = build(args.corpus, dim=args.dim, epochs=args.epochs, log=False)
        first = json.dumps(payload, indent=2)
        second = json.dumps(again, indent=2)
        digest = hashlib.sha256(first.encode("utf-8")).hexdigest()
        if first != second:
            # Distinguish the two causes, because they need opposite
            # responses. `store` and `lme` read fixed files, but `repo`
            # IS the working tree — editing any tracked docstring while
            # this runs changes the corpus underneath it, and that is a
            # dirty-tree error, not a trainer defect. The campaign
            # already voids artifacts on `tree_dirty` for the same
            # reason. (Observed on 2026-08-11: a test file edited
            # mid-run reported the trainer as non-deterministic.)
            same_corpus = (
                payload["corpus_manifest_sha256"] == again["corpus_manifest_sha256"]
            )
            if not same_corpus:
                print(
                    f"CORPUS CHANGED under the run: {args.corpus} manifest "
                    f"{payload['corpus_manifest_sha256'][:12]} -> "
                    f"{again['corpus_manifest_sha256'][:12]}. The tree was "
                    "edited mid-run; re-run on a quiet tree.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"NOT DETERMINISTIC: {args.corpus} differed between runs "
                    "on an identical corpus",
                    file=sys.stderr,
                )
            return 1
        print(f"deterministic: {args.corpus} sha256 {digest}", file=sys.stderr)
    out = Path(args.out).expanduser()
    if not out.is_absolute():
        out = (_HERE / out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
