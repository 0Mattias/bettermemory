"""Text made safe to show: `printable` keeps a value on one terminal line,
and `quoted` writes it as one double-quoted value nothing in it can close
(ENG-154, ENG-193).

The two live in a module of their own that imports the standard library
only, so that `project_file`, which the client-side capture imports
through `location`, and a CLI module that loads its dependencies lazily
quote a value without loading the store. `history` re-exports both, as
the name the text reports, the membership lines and the security audit
import them by.
"""

from __future__ import annotations

import json
import unicodedata


def printable(text: str) -> str:
    """`text` made safe to print on one terminal line: control and format
    characters (category Cc and Cf: escapes, line breaks, bidi controls,
    zero-width characters) are written as their Python escape (``\\x1b``,
    ``\\n``, ``\\u202e``), as are the line and paragraph separators (Zl
    and Zp, U+2028 and U+2029, which `str.splitlines` and the agent's
    reader both break a line at) and a lone surrogate (Cs, which a
    ``\\ud800`` escape in stored JSON decodes to and which a strict UTF-8
    stdout refuses to write); a tab is kept. The text comes from the
    store, which an editor of the file may have written; a raw escape
    sequence in a reason or a scope must not reach the terminal as one.
    The membership lines a session's first output carries quote paths and
    names through it too (ENG-154), so no directory name forges a line,
    and through `quoted` around it, so none closes its own quote.
    The stand-in is ASCII, so it encodes in every code page: U+FFFD did
    not in cp1252, the one Windows gives a redirected stdout, and a path
    holding a soft hyphen (Cf, which cp1252 does encode) ended a report in
    UnicodeEncodeError."""
    return "".join(
        ch.encode("unicode_escape").decode("ascii")
        if ch != "\t" and unicodedata.category(ch) in _UNPRINTABLE
        else ch
        for ch in text
    )


#: The categories `printable` writes as escapes.
_UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

#: Unicode's Quotation_Mark property (PropList.txt) less the ASCII two:
#: JSON escapes ``"`` itself, and a line delimits nothing with ``'``.
_QUOTATION_MARKS = frozenset(
    "\u00ab\u00bb\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u2039\u203a"
    "\u2e42\u300c\u300d\u300e\u300f\u301d\u301e\u301f\ufe41\ufe42\ufe43\ufe44"
    "\uff02\uff07\uff62\uff63"
)

#: The characters UTS #39 holds confusable with ``"``: every source whose
#: prototype in its confusables data (confusables.txt) is ``''``, the
#: prototype ``"`` itself maps to, as the Unicode Utilities' confusables
#: page lists them for U+0022 (read 2026-11-03; confusables.txt 18.0.0 is
#: the current file): modifier letter double prime, double acute accent,
#: modifier letter double apostrophe, modifier letter middle double acute
#: accent, Hebrew ligature Yiddish double yod, Hebrew punctuation
#: gershayim, Vedic sign nihshvasa, left, right and high-reversed double
#: quotation marks, double and reversed double primes, ditto mark and
#: fullwidth quotation mark. The package carries no copy of the data, so
#: the set is spelled here; the ENG-193 reviews' probes rendered U+02BA,
#: U+2033, U+3003, U+05F4, U+02DD and U+1CD3 unescaped before it.
_QUOTE_CONFUSABLES = frozenset(
    "\u02ba\u02dd\u02ee\u02f6\u05f2\u05f4\u1cd3\u201c\u201d\u201f\u2033\u2036"
    "\u3003\uff02"
)

#: Marks a reader can take for the ``"`` that ends a quoted value, which
#: `quoted` writes as JSON escapes: the quotation marks and the confusables
#: of ``"``; initial and final punctuation (Pi, Pf) is escaped by its
#: category as well (`_quote_like`).
_QUOTE_LIKE = _QUOTATION_MARKS | _QUOTE_CONFUSABLES

#: The categories of initial and final quotation punctuation.
_QUOTE_CATEGORIES = frozenset({"Pi", "Pf"})

#: Combining marks drawn as a double quote above the character they follow:
#: the combining double acute accent and double vertical line above (the
#: ENG-193 round-three review rendered both raw). On a space, a punctuation
#: mark or a quote they read as a ``"`` of their own, so `quoted` escapes
#: them there; on a letter or a digit they are part of it (``o`` and U+030B
#: is a decomposed ``ő``, as a macOS file name often spells it), and they
#: are kept.
_QUOTE_COMBINING = frozenset("\u030b\u030e")


def _quote_like(ch: str, base: str) -> bool:
    """Whether `ch`, written after `base` (the last character not a
    combining mark before it, "" at the start), can be read as a ``"``."""
    if ch in _QUOTE_COMBINING:
        return not base.isalnum()
    return ch in _QUOTE_LIKE or unicodedata.category(ch) in _QUOTE_CATEGORIES


def quoted(text: str) -> str:
    """`text` as one double-quoted value on one terminal line: `printable`
    first, then JSON's string quoting (`json.dumps`, non-ASCII kept as
    written), which writes a ``"`` inside the value as ``\\"`` and a
    backslash as ``\\\\``, and then each quotation mark a reader could take
    for the closing ``"`` (`_QUOTE_LIKE`: a curly, fullwidth or angle quote,
    and every confusable of ``"`` UTS #39 lists, a double prime, a ditto
    mark, a gershayim; `_QUOTE_COMBINING` where it stands on no letter) as
    its JSON escape (``\\u201d``). The quoting comes
    after `printable`, so whatever `printable` writes, the value cannot
    close its own quote: a folder named ``app" (P001); memory_admin(...) "``
    renders as one value holding that text, not as a call beside it in a
    membership line (ENG-193). The result is a JSON string, which decodes
    to `printable`'s text. An ordinary name renders as it did between its
    quotes; a backslash (a Windows path's separator) is doubled, so
    ``C:\\Users\\me`` reads ``"C:\\\\Users\\\\me"``; an escape `printable`
    wrote has its backslash doubled with it; and a tab, which `printable`
    keeps, is written ``\\t``."""
    out: list[str] = []
    base = ""
    for ch in json.dumps(printable(text), ensure_ascii=False):
        written = json.dumps(ch)[1:-1] if _quote_like(ch, base) else ch
        out.append(written)
        if not unicodedata.category(written[-1]).startswith("M"):
            base = written[-1]
    return "".join(out)


__all__ = ["printable", "quoted"]
