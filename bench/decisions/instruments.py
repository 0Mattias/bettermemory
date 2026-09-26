"""Uniform typed-decision interface over the instruments.

ask(state, questions) -> {key: {"noul": p} | {"choice": name, "probabilities": {opt: p}}}
questions: {key: {"type": "noul"|"choice", "instructions": str, "criteria": {...}}}

Local instruments (Kev through its server, SemIf through its CLI, Laya in
process) never leave the machine. Jev is TypeSafe's hosted model reached
through OpenRouter's TypeSafe-compatible endpoint: every state it judges is
sent to that service, so the drivers only point it at data the owner has
released for it. The key is read from OPENROUTER_API_KEY at construction
and is never written to any artifact.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

JEV_MODEL = "typesafe/jev-1.13"
JEV_URL = "https://openrouter.ai/api/v1/systemone"
JEV_USD_PER_INPUT_TOKEN = 0.042 / 1_000_000  # OpenRouter's listed price; output is free
JEV_TOKENS_PER_CHAR = 0.6  # the one fictional probe read 505 tokens for about 870 chars


def uniform(questions: dict, answers: dict) -> dict:
    """Map a System One style answers block onto the interface above."""
    res = {}
    for k, q in questions.items():
        a = answers.get(k) or {}
        if q["type"] == "noul":
            res[k] = {"noul": float(a.get("noul"))}
        else:
            res[k] = {
                "choice": a.get("choice"),
                "probabilities": {
                    o: float(p) for o, p in (a.get("probabilities") or {}).items()
                },
            }
    return res


def post_json(
    req: urllib.request.Request, timeout: int = 120, attempts: int = 3
) -> dict:
    """POST and decode, retrying network and 5xx failures; a 4xx is raised at once."""
    for attempt in range(attempts):
        try:
            return json.loads(
                urllib.request.urlopen(req, timeout=timeout).read().decode()
            )
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 or attempt == attempts - 1:
                raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:400]}") from e
        except Exception:
            if attempt == attempts - 1:
                raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def estimate_jev_cost(states: list[str], questions_per_state: list[dict]) -> dict:
    """Projected calls, tokens and dollars for a Jev run, before any call is made."""
    chars = sum(
        len(s) + len(json.dumps(q, ensure_ascii=False))
        for s, q in zip(states, questions_per_state)
    )
    tokens = int(chars * JEV_TOKENS_PER_CHAR)
    return {
        "calls": len(states),
        "chars": chars,
        "tokens_estimated": tokens,
        "usd_estimated": round(tokens * JEV_USD_PER_INPUT_TOKEN, 4),
        "basis": f"{JEV_TOKENS_PER_CHAR} tokens per character, ${JEV_USD_PER_INPUT_TOKEN * 1e6:.3f} per million input tokens",
    }


class Kev:
    name = "kev-0.8b"

    def __init__(self, url: str = "http://127.0.0.1:8009", model: str = "kev-latest"):
        self.url, self.model = url, model
        self.version = json.loads(
            urllib.request.urlopen(url + "/v1/models", timeout=10).read().decode()
        )

    def ask(self, state: str, questions: dict) -> dict:
        req = urllib.request.Request(
            self.url + "/v1/systemone",
            data=json.dumps(
                {"state": state, "model": self.model, "questions": questions}
            ).encode(),
            headers={"content-type": "application/json"},
        )
        out = post_json(req)
        return uniform(questions, out.get("answers") or out)


class Jev:
    """TypeSafe's Jev through OpenRouter. Stops itself once `max_cost` dollars have been spent."""

    name = "jev-1.13"

    def __init__(
        self,
        model: str = JEV_MODEL,
        url: str = JEV_URL,
        max_cost: float = 1.0,
    ):
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is not set")
        self._key = key
        self.url, self.model, self.max_cost = url, model, max_cost
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        self.version = {
            "model": model,
            "served": None,
            "provider": None,
            "endpoint": url,
        }

    def ask(self, state: str, questions: dict) -> dict:
        if self.usage["cost"] >= self.max_cost:
            raise RuntimeError(
                f"Jev spend guard: ${self.usage['cost']:.4f} spent, cap ${self.max_cost:.2f}"
            )
        req = urllib.request.Request(
            self.url,
            data=json.dumps(
                {"model": self.model, "state": state, "questions": questions},
                ensure_ascii=False,
            ).encode(),
            headers={
                "content-type": "application/json",
                "authorization": "Bearer " + self._key,
            },
        )
        out = post_json(req)
        u = out.get("usage") or {}
        self.usage["calls"] += 1
        self.usage["input_tokens"] += int(u.get("input_tokens") or 0)
        self.usage["output_tokens"] += int(u.get("output_tokens") or 0)
        self.usage["cost"] = round(self.usage["cost"] + float(u.get("cost") or 0.0), 8)
        self.version["served"] = out.get("model")
        self.version["provider"] = out.get("provider")
        return uniform(questions, out.get("answers") or {})


class SemIf:
    name = "semif-qwen3.5-4b"

    def __init__(
        self,
        exe: str,
        model: str = "Qwen/Qwen3.5-4B",
        revision: str = "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a",
        backend: str = "mlx",
        workdir: str | None = None,
    ):
        self.exe, self.model, self.revision, self.backend = (
            exe,
            model,
            revision,
            backend,
        )
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="semif-"))
        self.version = {"model": model, "revision": revision, "backend": backend}
        self._batch: list[tuple[str, dict]] = []

    # SemIf scores a JSONL file per call; the drivers batch all questions of a run and call flush().
    def queue(self, rid: str, state: str, questions: dict) -> None:
        self._batch.append((rid, {"state": state, "questions": questions}))

    def flush(self) -> dict[str, dict]:
        rows = []
        for rid, item in self._batch:
            for k, q in item["questions"].items():
                if q["type"] == "noul":
                    opts = [
                        {
                            "id": "yes",
                            "description": (q.get("criteria") or {}).get("true")
                            or "Yes, this is true.",
                        },
                        {
                            "id": "no",
                            "description": (q.get("criteria") or {}).get("false")
                            or "No, this is false.",
                        },
                    ]
                    rows.append(
                        {
                            "id": f"{rid}::{k}",
                            "state": item["state"],
                            "question": q["instructions"],
                            "options": opts,
                        }
                    )
                else:
                    opts = [
                        {"id": o, "description": d} for o, d in q["criteria"].items()
                    ]
                    rows.append(
                        {
                            "id": f"{rid}::{k}",
                            "state": item["state"],
                            "question": q["instructions"],
                            "options": opts,
                        }
                    )
        inp, outp = self.workdir / "in.jsonl", self.workdir / "out.jsonl"
        inp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        if outp.exists():
            outp.unlink()
        cmd = [
            self.exe,
            "--mode",
            "direct",
            "--backend",
            self.backend,
            "--model",
            self.model,
            "--revision",
            self.revision,
            "--input",
            str(inp),
            "--output",
            str(outp),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=7200)
        results: dict[str, dict] = {}
        for line in outp.read_text().splitlines():
            r = json.loads(line)
            rid, k = r["id"].split("::", 1)
            probs = _semif_probs(r)
            results.setdefault(rid, {})[k] = probs
        # convert to the uniform shape
        qmap = {rid: item["questions"] for rid, item in self._batch}
        out: dict[str, dict] = {}
        for rid, ans in results.items():
            out[rid] = {}
            for k, probs in ans.items():
                q = qmap[rid][k]
                if q["type"] == "noul":
                    out[rid][k] = {"noul": float(probs.get("yes", 0.0))}
                else:
                    best = max(probs, key=probs.get) if probs else None
                    out[rid][k] = {"choice": best, "probabilities": probs}
        self._batch = []
        return out


def _semif_probs(r: dict) -> dict[str, float]:
    """SemIf output rows carry option probabilities under one of a few keys; normalise."""
    if (
        isinstance(r.get("option_ids"), list)
        and isinstance(r.get("probabilities"), list)
        and r["probabilities"]
        and not isinstance(r["probabilities"][0], dict)
    ):
        return {str(o): float(p) for o, p in zip(r["option_ids"], r["probabilities"])}
    for key in ("probabilities", "option_probabilities", "probs"):
        v = r.get(key)
        if isinstance(v, dict):
            return {str(k): float(x) for k, x in v.items()}
        if isinstance(v, list):
            return {
                str(o.get("id")): float(o.get("probability", o.get("p", 0.0)))
                for o in v
                if isinstance(o, dict)
            }
    opts = r.get("options")
    if (
        isinstance(opts, list)
        and opts
        and isinstance(opts[0], dict)
        and any("probability" in o or "p" in o or "score" in o for o in opts)
    ):
        return {
            str(o.get("id")): float(
                o.get("probability", o.get("p", o.get("score", 0.0)))
            )
            for o in opts
        }
    raise ValueError(f"unknown semif row shape: {list(r.keys())}")


class Laya:
    name = "laya-english"

    def __init__(self, model: str = "english", max_len: int | None = None):
        from laya import Router

        self.router = Router(default=model)
        self.max_len = max_len
        self.version = {
            "package": __import__("importlib.metadata").metadata.version("laya"),
            "model": model,
        }

    def ask(self, state: str, questions: dict) -> dict:
        kw = {"max_len": self.max_len} if self.max_len else {}
        out = self.router.predict(state, questions, **kw)
        return uniform(questions, out.get("answers") or out)


class Chat:
    """The chat model itself, judging blind files in a session; answers are loaded from the driver's saved-answers file."""

    name = "claude-fable-5.1-in-session"
    version = {
        "model": "claude-fable-5-1",
        "shape": "blind judgement of the same states and questions, probabilities written by the model",
    }

    def ask(self, state, questions):
        raise RuntimeError(
            "chat arm answers must be supplied through the saved answers file"
        )

    def queue(self, *a, **k):
        raise RuntimeError(
            "chat arm answers must be supplied through the saved answers file"
        )

    def flush(self):
        raise RuntimeError(
            "chat arm answers must be supplied through the saved answers file"
        )


def make(name: str, args) -> object:
    """The instrument named on a driver's command line."""
    if name == "kev":
        return Kev()
    if name == "semif":
        return SemIf(args.semif_exe, backend=args.semif_backend)
    if name == "laya":
        return Laya(max_len=args.laya_max_len)
    if name == "jev":
        return Jev(max_cost=args.max_cost)
    return Chat()
