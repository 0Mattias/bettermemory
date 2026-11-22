"""Fixtures: the server builder's two pure schema scrubs
(`builder._strip_titles`, `builder._fold_nullable`).

One file, ``builder_schema.jsonl``, not gated, one JSON object per line,
keys sorted, ASCII. A case is ``tree`` (a JSON text, so the key order of
every object survives), with ``strip``, ``fold`` and ``both`` (the tree
after `_strip_titles`, after `_fold_nullable`, and after `_strip_titles`
then `_fold_nullable`, each as ``json.dumps`` writes it); an answer equal
to the one before it (``strip`` to ``tree``, ``fold`` to ``tree``,
``both`` to ``strip``) is left out. Every tree is
written by hand here; the last ones are copied once in the shape pydantic
emits for a tool's arguments, so no row follows the installed pydantic.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from bettermemory import builder

from ._common import jsonl

_NULL = {"type": "null"}


def _trees() -> list[Any]:
    return [
        # Not a dict or a list: nothing to do.
        None,
        "title",
        3,
        [],
        {},
        # The title of a node, at every depth, whatever its value.
        {"title": "X"},
        {"title": {"title": "inner"}, "type": "object"},
        {"type": "string", "title": "Content"},
        {"items": {"title": "Item", "type": "string"}, "title": "L"},
        [{"title": "a"}, [{"title": "b", "x": [{"title": "c"}]}]],
        # A default of None goes; any other default stays.
        {"default": None},
        {"default": None, "title": "T", "type": "integer"},
        {"default": False},
        {"default": 0},
        {"default": ""},
        {"default": []},
        {"default": {"default": None, "title": "kept?"}},
        {"default": [None, {"default": None}]},
        # The maps of caller-chosen names: descended by value only.
        {"properties": {"title": {"type": "string", "title": "Title"}}},
        {"properties": {"default": None, "x": {"default": None}}},
        {"$defs": {"title": {"title": "Def", "type": "object"}}},
        {"definitions": {"title": {"title": "Def"}, "n": 3}},
        {"properties": [{"title": "listed"}]},
        {"properties": "title"},
        {"properties": {"p": {"properties": {"title": {"title": "deep"}}}}},
        {"other": {"title": {"title": "named"}}},
        # The fold: the exact shape, either order.
        {"anyOf": [{"type": "string"}, _NULL]},
        {"anyOf": [_NULL, {"type": "integer"}]},
        {"anyOf": [_NULL, _NULL]},
        {"anyOf": [{"type": "string", "maxLength": 9}, _NULL], "description": "d"},
        {"description": "d", "anyOf": [{"description": "e", "type": "string"}, _NULL]},
        {
            "anyOf": [{"title": "S", "type": "string"}, _NULL],
            "title": "Opt",
            "default": None,
        },
        {
            "anyOf": [
                {"type": "array", "items": {"anyOf": [{"type": "string"}, _NULL]}},
                _NULL,
            ]
        },
        # Each guard of the fold.
        {"anyOf": [{"type": "string"}, {"type": "null", "title": "N"}]},
        {"anyOf": [{"type": "string"}, {"type": "NULL"}]},
        {"anyOf": [{"type": "string"}, {"type": None}]},
        {"anyOf": [{"type": "string"}, _NULL, _NULL]},
        {"anyOf": [_NULL]},
        {"anyOf": {"a": {"type": "string"}, "b": _NULL}},
        {"anyOf": ["string", _NULL]},
        {"anyOf": [{"type": ["string", "integer"]}, _NULL]},
        {"anyOf": [{"type": 1}, _NULL]},
        {"anyOf": [{"maxLength": 3}, _NULL]},
        {"anyOf": [{"$ref": "#/$defs/X", "type": "object"}, _NULL]},
        {"anyOf": [{"anyOf": [{"type": "string"}], "type": "string"}, _NULL]},
        {"anyOf": [{"type": "string"}, _NULL], "type": "string"},
        {"type": "object", "anyOf": [{"type": "string"}, _NULL]},
        {"anyOf": [{"type": "string", "anyOf": []}, _NULL]},
        # Inside the named maps, and a parameter named anyOf.
        {"properties": {"a": {"anyOf": [{"type": "string"}, _NULL]}}},
        {"properties": {"anyOf": [{"type": "string"}, _NULL]}},
        {"$defs": {"D": {"anyOf": [_NULL, {"type": "boolean"}]}}},
        {"x": [{"anyOf": [{"type": "number"}, _NULL]}]},
        # pydantic's shape for a tool's arguments (copied once).
        {
            "properties": {
                "content": {"title": "Content", "type": "string"},
                "project": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "title": "Project",
                },
                "limit": {"default": 30, "title": "Limit", "type": "integer"},
                "tags": {
                    "anyOf": [
                        {"items": {"type": "string"}, "type": "array"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "title": "Tags",
                },
                "title": {
                    "anyOf": [{"maxLength": 80, "type": "string"}, {"type": "null"}],
                    "default": None,
                    "title": "Title",
                },
                "mode": {"$ref": "#/$defs/Mode", "default": "fast"},
            },
            "required": ["content"],
            "title": "memory_writeArguments",
            "type": "object",
            "$defs": {
                "Mode": {"enum": ["fast", "full"], "title": "Mode", "type": "string"},
            },
        },
        {
            "properties": {
                "flag": {"default": False, "title": "Flag", "type": "boolean"},
                "since": {
                    "anyOf": [
                        {"format": "date-time", "type": "string"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "title": "Since",
                },
                "ref": {
                    "anyOf": [{"$ref": "#/$defs/Ref"}, {"type": "null"}],
                    "default": None,
                },
            },
            "title": "Args",
            "type": "object",
            "$defs": {
                "Ref": {
                    "properties": {"id": {"title": "Id", "type": "string"}},
                    "required": ["id"],
                    "title": "Ref",
                    "type": "object",
                }
            },
        },
    ]


def _after(tree: Any, *walks: Any) -> str:
    t = copy.deepcopy(tree)
    for walk in walks:
        walk(t)
    return json.dumps(t)


def generate() -> dict[str, bytes]:
    records = []
    for tree in _trees():
        text = json.dumps(tree)
        strip = _after(tree, builder._strip_titles)
        fold = _after(tree, builder._fold_nullable)
        both = _after(tree, builder._strip_titles, builder._fold_nullable)
        row = {"tree": text}
        if strip != text:
            row["strip"] = strip
        if fold != text:
            row["fold"] = fold
        if both != strip:
            row["both"] = both
        records.append(row)
    return {"builder_schema.jsonl": jsonl(records)}
