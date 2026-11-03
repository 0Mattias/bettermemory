"""Model-control tokens: the chat-template delimiters a stored text may
not carry (ENG-105, ENG-189).

`find_control_tokens` reads a text for the delimiters a model's chat
template uses to open or close a turn, name a role or end the text, and
names each by its template family and its bare name. memory_write
(`ControlTokenGate`), memory_update and the episode write refuse a body,
and an episode's takeaway, that carries one, each with its own hint and
with no flag.

`refuse_control_tokens` is the same refusal for the free-text arguments
an agent can store beside a body and a tool reads back (ENG-189):
`reason` on memory_remove and acknowledge_miss, the `note` of
memory_verify, memory_record_use and a conflict verdict,
memory_record_use's `claim_excerpts`, a link's `note` on memory_update,
`claims` (as given and in the form the record keeps) and the
`verified_*` attestations; and the handoff's explicit `prior_session_id`,
since no session id holds one and the event log would keep it. It raises
a teaching `ValueError` naming the field and each token by family and
bare name.

`defang_control_tokens` writes a text with each token as its bare name,
for the text that is kept rather than refused: the user's words a change
rests on (`folded_quote`), which must be quoted as typed, and an event
row's free-text fields. `named_tokens` writes every string of a value so,
for what a model reads: a tool's reply and refusal, the SDK's argument
errors and the hooks' text, which can carry a path, an id or a declared
name the store keeps as given, since it compares them.
`control_token_offsets` is what `bettermemory security scan` reports of
a text already stored: each family, where it first sits and how often,
never a token.

It imports the standard library and `_printable`, which imports the
standard library only, as `credentials.py` beside it imports nothing from
the package, so a surface that runs the check need not import the
write-gate chain to reach it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final, NamedTuple

from ._printable import quoted

#: A value `named_tokens` walks into.
_Container = dict[Any, Any] | list[Any] | tuple[Any, ...]

# The delimiters a model's chat template uses to open or close a turn,
# name a role or end the text. Every body the store keeps is read back
# into a later session's context (the recall hook, `memory_search`, the
# episode handoff), and a body that carries one can forge a turn boundary
# there: on a stack that parses special tokens out of plain text it IS
# the boundary, and to any model it reads as one. The legacy turn markers
# reach a model raw only where a body is read as plain text (the export
# mirror, which writes `---\n\n<body>`, or a CLI listing piped to a
# model); tool results carry them JSON-escaped, and the hooks' pointers
# flatten newlines. The ChatML note that introduced `<|im_start|>` names
# the hazard as injection from input carrying special-token syntax, and
# tiktoken's `encode` refuses such text unless the caller allows the
# token. So does every surface that runs this detector, with no flag: a
# body about a template loses nothing by naming the token without its
# delimiters (`im_start`), or by quoting a `Human:` line, and a flag the
# model can pass is no defence against a writer that was steered into
# writing the token.
#
# The tokens are the documented templates', matched exactly (a tokenizer
# reads them case-sensitively) and filed by family, the `kind` a refusal
# names. A token two families share is filed under the first:
#
# - chatml: `<|im_start|>` and `<|im_end|>` (OpenAI's ChatML note; Qwen's
#   template) and `<|im_sep|>` (Phi-4's model card).
# - openai: the special tokens tiktoken defines
#   (`tiktoken_ext/openai_public.py`): `<|endoftext|>`, `<|startoftext|>`,
#   `<|endofprompt|>` and the three fill-in-the-middle tokens.
# - harmony: gpt-oss's response format (OpenAI's harmony guide):
#   `<|start|>`, `<|end|>`, `<|message|>`, `<|channel|>`,
#   `<|constrain|>`, `<|return|>`, `<|call|>`. Phi-3's `<|end|>` is this
#   one.
# - llama3: Meta's Llama 3.1 prompt format: `<|begin_of_text|>`,
#   `<|end_of_text|>`, `<|start_header_id|>`, `<|end_header_id|>`,
#   `<|eot_id|>`, `<|eom_id|>`, `<|python_tag|>`,
#   `<|finetune_right_pad_id|>`. Granite's `<|end_of_text|>` is this one.
# - llama4: the Llama 4 text format's own: `<|header_start|>`,
#   `<|header_end|>`, `<|eot|>`.
# - llama2: `[INST]`, `[/INST]`, `<<SYS>>` and `<</SYS>>`, the
#   `SPECIAL_TAGS` of Meta's Llama 2 `generation.py`; Mistral's
#   tokenization guide documents the same instruction pair.
# - phi: Phi-3's model card: `<|system|>`, `<|user|>`, `<|assistant|>`.
# - gemma: Gemma's prompt structure: `<start_of_turn>`, `<end_of_turn>`.
# - command-r: Cohere's Command R template: `<|START_OF_TURN_TOKEN|>`,
#   `<|END_OF_TURN_TOKEN|>`, `<|SYSTEM_TOKEN|>`, `<|USER_TOKEN|>`,
#   `<|CHATBOT_TOKEN|>`.
# - granite: IBM Granite 3's chat template: `<|start_of_role|>`,
#   `<|end_of_role|>`, `<|tool_call|>`.
# - deepseek: DeepSeek-V3's chat template (its `tokenizer_config.json`),
#   spelled with the fullwidth bar and `▁`: the sentence's begin and end,
#   the `User` and `Assistant` turns and nine tool-call delimiters.
# - anthropic: the legacy Text Completions turn markers `\n\nHuman:` and
#   `\n\nAssistant:` (the Python SDK's `HUMAN_PROMPT` and `AI_PROMPT`),
#   at a body's start, after any leading whitespace (the store keeps a
#   body stripped, so `  Human:` is stored opening with `Human:`), or
#   after a blank line.
#
# Left out on purpose, each because ordinary text spells it: `<s>` and
# `</s>` (Llama 2's and Mistral's sentence tokens, and HTML's
# strikethrough tags), Gemma's `<bos>` and `<eos>` (the notation of every
# paper about tokenizers), Qwen's `<think>` and `<tool_call>` tags, and
# Mistral's later bracketed tokens (`[TOOL_CALLS]`, `[AVAILABLE_TOOLS]`,
# `[SYSTEM_PROMPT]` and kin), which read as a template's placeholders and
# which the bracket shape below cannot match anyway (its name class holds
# no `_`). The Llama 4 vision format's image tokens mark where an image
# sits and open no turn.
_CONTROL_TOKENS: dict[str, tuple[str, ...]] = {
    "chatml": ("<|im_start|>", "<|im_end|>", "<|im_sep|>"),
    "openai": (
        "<|endoftext|>",
        "<|startoftext|>",
        "<|endofprompt|>",
        "<|fim_prefix|>",
        "<|fim_middle|>",
        "<|fim_suffix|>",
    ),
    "harmony": (
        "<|start|>",
        "<|end|>",
        "<|message|>",
        "<|channel|>",
        "<|constrain|>",
        "<|return|>",
        "<|call|>",
    ),
    "llama3": (
        "<|begin_of_text|>",
        "<|end_of_text|>",
        "<|start_header_id|>",
        "<|end_header_id|>",
        "<|eot_id|>",
        "<|eom_id|>",
        "<|python_tag|>",
        "<|finetune_right_pad_id|>",
    ),
    "llama4": ("<|header_start|>", "<|header_end|>", "<|eot|>"),
    "llama2": ("[INST]", "[/INST]", "<<SYS>>", "<</SYS>>"),
    "phi": ("<|system|>", "<|user|>", "<|assistant|>"),
    "gemma": ("<start_of_turn>", "<end_of_turn>"),
    "command-r": (
        "<|START_OF_TURN_TOKEN|>",
        "<|END_OF_TURN_TOKEN|>",
        "<|SYSTEM_TOKEN|>",
        "<|USER_TOKEN|>",
        "<|CHATBOT_TOKEN|>",
    ),
    "granite": ("<|start_of_role|>", "<|end_of_role|>", "<|tool_call|>"),
    "deepseek": (
        "<｜begin▁of▁sentence｜>",
        "<｜end▁of▁sentence｜>",
        "<｜User｜>",
        "<｜Assistant｜>",
        "<｜tool▁calls▁begin｜>",
        "<｜tool▁calls▁end｜>",
        "<｜tool▁call▁begin｜>",
        "<｜tool▁call▁end｜>",
        "<｜tool▁sep｜>",
        "<｜tool▁outputs▁begin｜>",
        "<｜tool▁outputs▁end｜>",
        "<｜tool▁output▁begin｜>",
        "<｜tool▁output▁end｜>",
    ),
}
#: Each listed token's family.
_CONTROL_TOKEN_KIND: dict[str, str] = {
    token: kind for kind, tokens in _CONTROL_TOKENS.items() for token in tokens
}
#: The family of the legacy turn markers, which `_CONTROL_TOKEN_RE` reads
#: by their own shape rather than from the table.
_TURN_MARKER_KIND = "anthropic"

# One pattern reads every delimiter shape the table uses, each with a
# bounded bare name, and the table decides: a match whose token no family
# lists (`<b>`, `[X]`, `<|foo|>`) is passed over. Linear time: every
# attempt starts on an opening delimiter (`<`, `[`, a newline or the
# body's start), a name is at most 32 characters of a class that holds
# neither its closing delimiter nor any opening one, so an attempt reads
# at most 37 characters (`<</` and `>>` around 32), and the two
# unbounded runs before a turn marker, the body's leading whitespace and
# the blank line's spaces, are read from the body's start and from the
# newline before them, and from no other start. The leading whitespace
# is read because the store keeps a body stripped: `  Human:` is stored
# opening with `Human:`. The blank line may hold any carriage returns
# among its spaces and tabs because the export mirror writes a body
# through `_frontmatter.normalise_body`, which strips the `\r`s that end
# each line: `x\n\r\r\nHuman:` is written `x\n\nHuman:`, a marker after a
# blank line. A line the mirror writes blank was spaces, tabs and `\r`s
# in the body, which this reads as blank too, so a body this reads clean
# is clean in the mirror (ENG-189). About 17 ms on 1 MB of prose and 60 ms on 1 MB
# of nothing but `<`, the costliest opener, each 2.0x at twice the
# length (measured by hand; `test_a_hostile_body_is_read_in_linear_time`
# pins a 2 s ceiling). A shape match the table passes over hides no
# listed token, since its name holds no opening delimiter.
_CONTROL_TOKEN_RE = re.compile(
    r"<\|(?P<pipe>[A-Za-z_]{1,32})\|>"
    r"|<｜(?P<wide>[^｜<>\[\]\s]{1,32})｜>"
    r"|<<(?P<double>/?[A-Z]{1,32})>>"
    r"|<(?P<angle>[a-z_]{1,32})>"
    r"|\[(?P<bracket>/?[A-Z]{1,32})\]"
    r"|(?:\A\s*|\n[ \t\r]*\n)(?P<turn>Human|Assistant):"
)


@dataclass(frozen=True)
class ControlTokenHit:
    """One control token a body carries: its family, and its bare name,
    the token without its delimiters (`im_start`, `/INST`, `Human`), so a
    refusal names the token without handing it back to the caller."""

    kind: str
    token: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "token": self.token}


def _matches(text: str) -> Iterator[tuple[int, str, str]]:
    """(offset, family, bare name) of every listed control token in
    `text`, in order. The offset is where the token starts: its opening
    delimiter, or a turn marker's name (`Human`), past the leading
    whitespace or the blank line the pattern reads before it."""
    for match in _CONTROL_TOKEN_RE.finditer(text):
        name = match.lastgroup
        if name is None:
            continue
        if name == "turn":
            kind: str | None = _TURN_MARKER_KIND
            start = match.start(name)
        else:
            kind = _CONTROL_TOKEN_KIND.get(match.group(0))
            start = match.start()
        if kind is None:
            continue
        yield start, kind, match.group(name)


def find_control_tokens(text: str) -> list[ControlTokenHit]:
    """Every listed control token in `text`, once each, in the order the
    body first carries them; empty when it carries none.

    `ControlTokenGate` (handlers/write_gates.py) runs it over a memory
    body, `memory_update` over an edited body, `episode_write` over an
    episode's body and takeaway, and `refuse_control_tokens` over a
    free-text argument."""
    hits: dict[tuple[str, str], ControlTokenHit] = {}
    for _start, kind, bare in _matches(text):
        hits.setdefault((kind, bare), ControlTokenHit(kind=kind, token=bare))
    return list(hits.values())


def control_token_offsets(text: str) -> dict[str, tuple[int, int]]:
    """Family -> (offset of its first token, how many tokens of it) in
    `text`, the families in the order the text first carries them; empty
    when it carries none.

    `bettermemory security scan` reports a stored text by it
    (`secret_scan`, ENG-189): the family, which every refusal names too,
    and an offset that locates the token for `memory_show` without
    carrying it, as the scan's credential findings do. Never the token,
    nor its bare name: the scan's output goes to a terminal, a ticket or
    a CI log, which a model may read next."""
    out: dict[str, tuple[int, int]] = {}
    for start, kind, _bare in _matches(text):
        first, count = out.get(kind, (start, 0))
        out[kind] = (first, count + 1)
    return out


# ---------------------------------------------------------------------------
# Text kept as given, each token by its bare name (ENG-189)
# ---------------------------------------------------------------------------


def _touches(ch: str, *, apart: bool, after: bool) -> bool:
    """Whether a bare name written beside `ch` is set apart from it by a
    space: beside a word character (`<|im_start|>system` reads `im_start
    system`, not one word), and before a delimiter that may open the next
    token (`<|im_start|><|im_end|>` reads `im_start im_end`); `apart`,
    beside anything but whitespace."""
    if apart:
        return not ch.isspace()
    return ch.isalnum() or ch == "_" or (after and ch in "<[")


#: The bare names a turn marker is spelled with (`_CONTROL_TOKEN_RE`'s
#: ``turn``). Written, each is set apart from whatever but whitespace
#: follows it: `<｜Assistant｜>：` (DeepSeek's token, then a fullwidth
#: colon) written `Assistant：` holds no marker, and NFKC, which folds the
#: colon to `:`, makes it `Assistant:` (the round-2 review's NFKC probe,
#: the one such shape in 400,000 random cases). Nothing folds a text a
#: model reads, but a space keeps any fold from spelling a marker.
_SPEAKERS = frozenset({"Human", "Assistant"})


def _named(match: re.Match[str], *, apart: bool) -> str:
    """One match of `_CONTROL_TOKEN_RE` as `defang_control_tokens` writes
    it: a listed token as its bare name, a turn marker as its name without
    the colon (its leading whitespace or blank line kept), each set apart
    from a character it touches (`_touches`), and a speaker's name
    (`_SPEAKERS`) from anything but whitespace after it; a shape no family
    lists as it is."""
    name = match.lastgroup
    whole = match.group(0)
    if name is None or (name != "turn" and whole not in _CONTROL_TOKEN_KIND):
        return whole
    text, start, end = match.string, match.start(), match.end()
    speaker = match.group(name) in _SPEAKERS
    after = end < len(text) and _touches(text[end], apart=apart or speaker, after=True)
    gap = " " if after else ""
    if name == "turn":
        # Nothing but whitespace or the text's start comes before it.
        return whole[: match.end(name) - start] + gap
    before = start > 0 and _touches(text[start - 1], apart=apart, after=False)
    return f"{' ' if before else ''}{match.group(name)}{gap}"


def defang_control_tokens(text: str) -> str:
    """`text` with each listed control token written as its bare name
    (`<|im_start|>` as `im_start`, `[INST]` as `INST`) and a turn marker
    as its name without the colon; `text` itself when it carries none.

    For a text kept as it was given rather than refused (ENG-189): the
    user's words a change rests on (`folded_quote`), which no rewrite may
    alter, an event row's free-text fields (`events.NAMED_EVENT_FIELDS`),
    and what a model reads (`named_tokens`): a tool's reply or refusal,
    the SDK's argument errors and the hooks' text, which can quote what
    the caller sent or a path, an id or a name the store keeps as given.
    The bare name is what every refusal names a token by.

    The result carries no listed token. A name is set apart by a space
    from a word character it touches, and that first pass is kept when
    the detector finds nothing in it; a text that nests one token in
    another's delimiters (`<|<|im_start|>|>` would read `<|im_start|>`)
    takes a second pass, which sets each name apart from everything but
    whitespace. No token holds whitespace or starts inside a bare name,
    so none can then span a name, and a name is never followed by a colon
    that would make it a turn marker. Linear: the pattern is read at most
    four times."""
    if not find_control_tokens(text):
        return text
    near = _CONTROL_TOKEN_RE.sub(lambda m: _named(m, apart=False), text)
    if not find_control_tokens(near):
        return near
    return _CONTROL_TOKEN_RE.sub(lambda m: _named(m, apart=True), text)


def folded_quote(text: str) -> str:
    """The user's words as the store keeps a quote of them (ENG-189):
    whitespace folded, as the prompt hook's check folds a quote
    (`recall.WordsSeen.holds`), each control token in them written as its
    bare name (`defang_control_tokens`), and folded again for the spaces
    that sets around a name. A quote must be the user's words as typed, so
    a token in one is kept by its name rather than refused: "drop [INST]"
    holds no part of eight characters or more that leaves the token out,
    and a quote cut short of the token may not say what the user said."""
    return " ".join(defang_control_tokens(" ".join(text.split())).split())


#: The end of a container's children, in `named_tokens`'s walk.
_DONE: Final = object()


def _children(node: _Container) -> Iterator[Any]:
    """A container's children in order: a dict's keys and values in turn."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield value
    else:
        yield from node


class _Visit(NamedTuple):
    """One container on `named_tokens`'s stack: the children still to read
    and what each child read became."""

    node: _Container
    rest: Iterator[Any]
    done: list[Any]


def _rebuilt(node: _Container, parts: list[Any]) -> Any:
    """`node` with `parts` for its children: `node` itself when each part
    is the child it was, so a value without a token is passed through."""
    if all(new is old for new, old in zip(parts, _children(node), strict=True)):
        return node
    if isinstance(node, dict):
        return dict(zip(parts[::2], parts[1::2], strict=True))
    return tuple(parts) if isinstance(node, tuple) else parts


def named_tokens(value: Any) -> Any:
    """`value` with every string in it, a dict's keys included and at any
    depth, written by `defang_control_tokens` (ENG-189); a container is
    rebuilt only when a string in it changes.

    What a model reads, whatever the store kept: a tool's reply
    (`_handlers._refusals_as_tool_errors`), which can name a checkout's
    path, a declared client or model, an id the caller sent or a body
    written before 9.0.0rc14; and an event row's free-text fields as the
    writer keeps them (`events.NAMED_EVENT_FIELDS`). A value is a tree, as
    JSON reads one and a reply is built.

    Naming is exact for JSON-shaped values alone (dicts, lists, tuples,
    strings, numbers, booleans, None): outside them, dict keys that name
    to one text merge, a changed NamedTuple or OrderedDict comes back a
    plain tuple or dict, and a set, bytes, a Path or a model passes
    through unnamed. The round-3 review's spy over 1,076 handler replies
    saw none of these.

    The walk keeps its own stack, as `secret_scan._strings` does, so a
    value nested as deep as `json.loads` reads one (about 990 levels; the
    interpreter's frames stop near 500 for a walk that recurses) is named
    rather than raising RecursionError (the round-2 review's depth
    probe)."""
    if isinstance(value, str):
        return defang_control_tokens(value)
    if not isinstance(value, (dict, list, tuple)):
        return value
    stack = [_Visit(value, _children(value), [])]
    while True:
        top = stack[-1]
        child = next(top.rest, _DONE)
        if child is _DONE:
            stack.pop()
            made = _rebuilt(top.node, top.done)
            if not stack:
                return made
            stack[-1].done.append(made)
        elif isinstance(child, (dict, list, tuple)):
            stack.append(_Visit(child, _children(child), []))
        else:
            top.done.append(
                defang_control_tokens(child) if isinstance(child, str) else child
            )


# ---------------------------------------------------------------------------
# The free-text arguments (ENG-189)
# ---------------------------------------------------------------------------

#: Why a free-text argument carrying a token is refused, in every such
#: refusal's words. The store keeps each argument `refuse_control_tokens`
#: reads, and the tools put most of them back in front of a model as
#: written: a tombstone's reason in a later write's `previously_removed`
#: matches, a link's or a conflict verdict's note, a claim and an
#: attestation in `memory_show`, a use's note and claim excerpt in
#: memory_search's `recent_negative_outcomes`, a use's and a verify's
#: note in the health report's `resolution_timeline`. Each one the signed
#: log records stays there, since no path rewrites the log. A flag would
#: pass a steered writer as readily as an honest one, as on the body
#: (ENG-105). The user's words are not refused but kept by name
#: (`folded_quote`): an agent can rewrite its own note, not the user's.
_FIELD_WHY = (
    "The store keeps this text, and a later read can put it in front of a "
    "model, where the token can forge a turn boundary, so no flag passes "
    "this refusal."
)

#: The remedy for a prose field, in the body refusals' terms
#: (`write_gates._CONTROL_TOKEN_HINT`): the bare name for a delimited
#: token, and for a turn marker, which has no delimiters to drop, a
#: quoted line.
BARE_NAME_REMEDY = (
    "Write the token's bare name without its delimiters (`im_start`, "
    "`INST`) or describe it in words; a token of kind `anthropic` is "
    "`Human:` or `Assistant:` opening the text or a line after a blank "
    "line: quote the line (`> Human: ...`) or name the speaker another way."
)


class ControlTokenError(ValueError):
    """A free-text argument carries a chat-template control token: a
    teaching error, and nothing is written. A `ValueError`, so the tool
    boundary answers it with its text, as it answers every argument's
    refusal."""


def refuse_control_tokens(
    field: str,
    text: str | None,
    *,
    remedy: str = BARE_NAME_REMEDY,
    why: str = _FIELD_WHY,
) -> None:
    """Raise `ControlTokenError` when `text`, the argument `field`,
    carries a listed control token; return when it carries none or is
    None or empty.

    The message names the field and each token by its family and its bare
    name, the name quoted (`_printable.quoted`), and never the token: a
    refusal is text the agent reads, which is where the token must not go
    back. `remedy` is the field's way through, `BARE_NAME_REMEDY` unless
    the field cannot take a bare name (a quote checked word for word, a
    path, a claim); `why` is why it is refused, `_FIELD_WHY` unless the
    field is no text a later read puts in front of a model (an id)."""
    if not text:
        return
    hits = find_control_tokens(text)
    if not hits:
        return
    named = ", ".join(f"{hit.kind} {quoted(hit.token)}" for hit in hits)
    raise ControlTokenError(
        f"`{field}` carries a chat-template control token, named here by "
        f"template family and bare name: {named}. {why} {remedy} "
        "Then send the call again."
    )


__all__ = [
    "BARE_NAME_REMEDY",
    "ControlTokenError",
    "ControlTokenHit",
    "control_token_offsets",
    "defang_control_tokens",
    "find_control_tokens",
    "folded_quote",
    "named_tokens",
    "refuse_control_tokens",
]
