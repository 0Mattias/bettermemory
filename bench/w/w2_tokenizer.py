"""The W2 subword vocabulary: byte-pair encoding, this repository's own.

The W2 declaration §2 declares the tokenizer: byte-pair
encoding learned from the pretraining slice by committed code,
deterministic merge order, lowercased input. The pre-tokenization is
the committed `w1_corpus.tokenize` — the same word stream the register
cache stores — and BPE runs within words, so a word's subword
decomposition is a pure function of the learned merge table and the
word's characters. The module is deliberately numpy-free: the CI leg
imports it in-process (the test venv carries no numpy), and the
trainer imports it under its own BLAS pins.

Determinism: merges are learned over the word-type table (type ->
count), and each step merges the pair with the highest weighted count,
ties broken lexicographically on the pair. Count-then-lexical is the
family's vocabulary order since W1, applied here to pairs; two trains
over the same type table emit byte-identical vocab and merge files.

The pair-row grammar for the census's derived TSV lives here too
(`parse_pair_row`, `keep_pair`), so the CI leg can exercise the keep
rule on hand-written rows without corpus bytes — the w3p2 idiom.
"""

from __future__ import annotations

import heapq
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

# Special tokens, in id order. [pad] must be id 0: the trainer's mask
# arithmetic and the pooled mean both key on nonzero ids.
PAD, MASK, UNK = "[pad]", "[mask]", "[unk]"
SPECIALS: tuple[str, ...] = (PAD, MASK, UNK)

# The end-of-word marker. Appended to every word's symbol sequence so a
# merge can distinguish a word-final fragment from a word-internal one.
_EOW = "</w>"

# Word types below this count do not vote on merges. A code constant,
# not a knob: the tail of the type table is dominated by typos and
# fused tokens whose votes are noise, and dropping them bounds the
# training structures without measurably moving the learned table.
_BPE_MIN_TYPE_COUNT = 3


def word_symbols(word: str) -> tuple[str, ...]:
    """A word's initial symbol sequence: characters plus the terminal."""
    return (*word, _EOW)


def train_bpe(
    type_counts: Counter[str] | dict[str, int], vocab_size: int
) -> tuple[list[str], list[tuple[str, str]]]:
    """Learn the merge table; return (vocab in id order, merges in rank order).

    The vocab is SPECIALS, then the single-symbol alphabet in
    count-then-lexical order, then one token per merge in merge order,
    capped at `vocab_size`. Merging stops when the cap is reached or no
    pair occurs twice.
    """
    words: list[list[str]] = []
    freqs: list[int] = []
    for word, count in sorted(type_counts.items()):
        if count >= _BPE_MIN_TYPE_COUNT:
            words.append(list(word_symbols(word)))
            freqs.append(int(count))

    alphabet: Counter[str] = Counter()
    for symbols, freq in zip(words, freqs):
        for symbol in symbols:
            alphabet[symbol] += freq
    vocab: list[str] = list(SPECIALS) + [
        s for s, _ in sorted(alphabet.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_words: dict[tuple[str, str], set[int]] = {}
    for wi, (symbols, freq) in enumerate(zip(words, freqs)):
        for pair in zip(symbols, symbols[1:]):
            pair_counts[pair] += freq
            pair_words.setdefault(pair, set()).add(wi)

    # Lazy max-heap over (-count, pair): every count change pushes a new
    # entry, and stale entries are discarded on pop by comparing against
    # the live count. Same argmax as a full scan — count-then-lexical,
    # because heapq orders (-count, pair) tuples exactly that way — at a
    # cost that scales with updates rather than with distinct pairs.
    heap: list[tuple[int, tuple[str, str]]] = [
        (-count, pair) for pair, count in pair_counts.items()
    ]
    heapq.heapify(heap)

    merges: list[tuple[str, str]] = []
    while len(vocab) < vocab_size and heap:
        neg_count, best = heapq.heappop(heap)
        live = pair_counts.get(best, 0)
        if live != -neg_count:
            if live >= 2:
                heapq.heappush(heap, (-live, best))
            continue
        if live < 2:
            break
        merges.append(best)
        vocab.append(best[0] + best[1])
        merged = best[0] + best[1]
        for wi in sorted(pair_words.get(best, ())):
            symbols = words[wi]
            freq = freqs[wi]
            for pair in zip(symbols, symbols[1:]):
                pair_counts[pair] -= freq
                if pair_counts[pair] <= 0:
                    del pair_counts[pair]
                remaining = pair_words.get(pair)
                if remaining is not None:
                    remaining.discard(wi)
                    if not remaining:
                        del pair_words[pair]
            out: list[str] = []
            i = 0
            while i < len(symbols):
                if (
                    i + 1 < len(symbols)
                    and symbols[i] == best[0]
                    and symbols[i + 1] == best[1]
                ):
                    out.append(merged)
                    i += 2
                else:
                    out.append(symbols[i])
                    i += 1
            words[wi] = out
            for pair in zip(out, out[1:]):
                pair_counts[pair] += freq
                pair_words.setdefault(pair, set()).add(wi)
                heapq.heappush(heap, (-pair_counts[pair], pair))
    return vocab[:vocab_size], merges


class Bpe:
    """Encode words against a learned merge table, with a per-word memo."""

    def __init__(self, vocab: list[str], merges: list[tuple[str, str]]) -> None:
        self.vocab = vocab
        self.token_to_id = {t: i for i, t in enumerate(vocab)}
        self.merge_rank = {pair: rank for rank, pair in enumerate(merges)}
        self.unk_id = self.token_to_id[UNK]
        self._memo: dict[str, tuple[int, ...]] = {}

    def encode_word(self, word: str) -> tuple[int, ...]:
        cached = self._memo.get(word)
        if cached is not None:
            return cached
        symbols = list(word_symbols(word))
        while len(symbols) > 1:
            ranked = [
                (self.merge_rank[pair], i)
                for i, pair in enumerate(zip(symbols, symbols[1:]))
                if pair in self.merge_rank
            ]
            if not ranked:
                break
            _, at = min(ranked)
            symbols[at : at + 2] = [symbols[at] + symbols[at + 1]]
        ids = tuple(self.token_to_id.get(s, self.unk_id) for s in symbols)
        self._memo[word] = ids
        return ids

    def encode_words(self, tokens: Iterable[str]) -> list[int]:
        out: list[int] = []
        for token in tokens:
            out.extend(self.encode_word(token))
        return out


def save_tokenizer(
    out_dir: Path, vocab: list[str], merges: list[tuple[str, str]]
) -> None:
    (out_dir / "vocab.txt").write_text("\n".join(vocab) + "\n", encoding="utf-8")
    (out_dir / "merges.txt").write_text(
        "".join(f"{a} {b}\n" for a, b in merges), encoding="utf-8"
    )


def load_tokenizer(out_dir: Path) -> Bpe:
    vocab = (out_dir / "vocab.txt").read_text(encoding="utf-8").splitlines()
    merges = [
        (line.split(" ", 1)[0], line.split(" ", 1)[1])
        for line in (out_dir / "merges.txt").read_text(encoding="utf-8").splitlines()
        if line
    ]
    return Bpe(vocab, merges)


# --- the pair-row grammar ------------------------------------------------

# The census's derived TSV (the W2 SO-census declaration §4):
# site, post id, related id, left prose, right prose, left markup-text,
# right markup-text. The trainer reads the prose columns.
_PAIR_COLUMNS = 7


def parse_pair_row(line: str) -> tuple[str, str] | None:
    """One derived-TSV line -> (left prose, right prose), or None.

    A row with the wrong column count is a malformed line and is
    rejected loudly by the caller counting rejects; this function only
    says whether the grammar held.
    """
    parts = line.rstrip("\n").split("\t")
    if len(parts) != _PAIR_COLUMNS:
        return None
    return parts[3], parts[4]


def keep_pair(
    left_ids: tuple[int, ...] | list[int],
    right_ids: tuple[int, ...] | list[int],
    min_tokens: int,
) -> bool:
    """The W2 declaration §3's keep rule: both sides at the floor."""
    return len(left_ids) >= min_tokens and len(right_ids) >= min_tokens
