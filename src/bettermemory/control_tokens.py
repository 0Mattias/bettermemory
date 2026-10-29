"""Model-control tokens: the chat-template delimiters a stored body may
not carry (ENG-105).

`find_control_tokens` reads a text for the delimiters a model's chat
template uses to open or close a turn, name a role or end the text, and
names each by its template family and its bare name. memory_write
(`ControlTokenGate`), memory_update and the episode write refuse a body
that carries one, each with its own hint and with no flag.

Standard library only, like `credentials.py` beside it, so a surface
that runs the check need not import the write-gate chain to reach it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


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
# opening with `Human:`. About 17 ms on 1 MB of prose and 60 ms on 1 MB
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
    r"|(?:\A\s*|\n[ \t]*\r?\n)(?P<turn>Human|Assistant):"
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


def find_control_tokens(text: str) -> list[ControlTokenHit]:
    """Every listed control token in `text`, once each, in the order the
    body first carries them; empty when it carries none.

    `ControlTokenGate` (handlers/write_gates.py) runs it over a memory
    body, `memory_update` over an edited body and `episode_write` over an
    episode's body and takeaway."""
    hits: dict[tuple[str, str], ControlTokenHit] = {}
    for match in _CONTROL_TOKEN_RE.finditer(text):
        name = match.lastgroup
        if name is None:
            continue
        if name == "turn":
            kind: str | None = _TURN_MARKER_KIND
        else:
            kind = _CONTROL_TOKEN_KIND.get(match.group(0))
        if kind is None:
            continue
        bare = match.group(name)
        hits.setdefault((kind, bare), ControlTokenHit(kind=kind, token=bare))
    return list(hits.values())
