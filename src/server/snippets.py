"""
Snippet extraction for search results.

Given a document's text and a query, find the passages where the query terms
occur and return them best first, each with a fixed number of words of
context before the first and after the last matched word.

Matching is stem-based on both sides (PyStemmer "porter", the same algorithm
family as Lucene's PorterStemFilter), so a query for "resistance" matches
"resistant" in the text.

Passage score (0..1) = (distinct query terms in the passage + density) /
(query terms + 1), where density is the mean over the matched terms of
tf / (tf + 1): a term seen once adds 0.5, twice 0.667, ten times 0.909. One
more distinct term therefore always outweighs any amount of repetition. The
BM25 score is per paper and lives on the document, not here.

This module has no dependency on the JVM / pyserini and is unit-testable on
its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import Stemmer
import orjson

# Word = run of letters/digits; splits on hyphen/underscore/punctuation like
# Lucene's StandardTokenizer does ("branched-chain" -> "branched", "chain").
WORD_RE = re.compile(r"[^\W_]+")

# Lucene's default English stop set (EnglishAnalyzer.ENGLISH_STOP_WORDS_SET).
STOPWORDS: Set[str] = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "if", "in",
    "into", "is", "it", "no", "not", "of", "on", "or", "such", "that", "the",
    "their", "then", "there", "these", "they", "this", "to", "was", "will",
    "with",
}

ELLIPSIS = "…"  # "…"

# Matches closer together than this (in characters) are grouped into one
# passage; matches further apart become separate passages.
CLUSTER_SPAN_CHARS = 300

# Only scan this much text per document when looking for matches. Papers are
# typically 20-200 KB; a hard cap keeps worst-case cost bounded.
MAX_SCAN_CHARS = 2_000_000

# Rough upper bound on characters per word (incl. the following space), used
# to bound the slice scanned when counting context words.
CHARS_PER_WORD = 20


def make_stemmer() -> "Stemmer.Stemmer":
    """A PyStemmer instance. Not thread-safe: create one per thread/request."""
    return Stemmer.Stemmer("porter")


def query_stems(query: str, stemmer: "Stemmer.Stemmer") -> Set[str]:
    """Lowercase, tokenize, drop stopwords and stem the query."""
    words = [w.lower() for w in WORD_RE.findall(query)]
    words = [w for w in words if w not in STOPWORDS]
    return set(stemmer.stemWords(words))


@dataclass(frozen=True)
class Match:
    start: int  # char offset of the matched word in the text
    end: int
    stem: str


@dataclass(frozen=True)
class Passage:
    content: str
    score: float      # (distinct terms + mean tf/(tf+1)) / (query terms + 1), 0..1
    hits: int         # occurrences of query terms inside the passage
    start: int        # char range in the source text (before cleaning)
    end: int


@lru_cache(maxsize=256)
def _candidate_pattern(stems: frozenset) -> "re.Pattern[str]":
    """
    Regex matching whole words that *could* stem to one of ``stems``.

    A Porter stem is a prefix of the original word in nearly all cases
    ("resist" -> resistance, "branch" -> branched); the main exception is the
    y->i rule ("studi" for study/studies), so a trailing "i" is also tried as
    "y". Candidates are then actually stemmed and checked, so this is only a
    cheap C-speed prefilter: it avoids stemming the ~13k words of a typical
    paper and only touches the few dozen that start like a query term.
    """
    alts = set()
    for s in stems:
        alts.add(re.escape(s))
        if s.endswith("i"):
            alts.add(re.escape(s[:-1] + "y"))
    body = "|".join(sorted(alts, key=len, reverse=True))
    # \b anchors at a word start; measured ~20% faster than an equivalent
    # lookbehind over ~1 MB of text per request. Stems are alphanumeric, so
    # \b before them means "preceded by a non-word char or start of text".
    return re.compile(r"\b(?:%s)[^\W_]*" % body, re.IGNORECASE)


def find_matches(
    text: str,
    stems: Set[str],
    stemmer: "Stemmer.Stemmer",
    start_at: int = 0,
    end_at: Optional[int] = None,
) -> List[Match]:
    """Every word in ``text[start_at:end_at]`` whose stem is one of ``stems``."""
    if not stems or not text:
        return []
    limit = min(len(text), start_at + MAX_SCAN_CHARS)
    if end_at is not None:
        limit = min(limit, max(start_at, end_at))
    pattern = _candidate_pattern(frozenset(stems))
    tokens = [(m.start(), m.end(), m.group().lower()) for m in pattern.finditer(text, start_at, limit)]
    if not tokens:
        return []
    stemmed = stemmer.stemWords([t[2] for t in tokens])
    return [Match(s, e, st) for (s, e, _), st in zip(tokens, stemmed) if st in stems]


def best_cluster(matches: Sequence[Match], span_chars: int) -> Optional[tuple[int, int]]:
    """
    Indices (i, j) into ``matches`` of the densest group whose matched words
    all lie within ``span_chars``. Score = distinct stems covered, then total
    matches; ties go to the earliest group.
    """
    if not matches:
        return None
    best = None  # ((distinct, total, -i), i, j)
    counts: Dict[str, int] = {}
    distinct = 0
    j = 0
    for i in range(len(matches)):
        while j < len(matches) and matches[j].end - matches[i].start <= span_chars:
            counts[matches[j].stem] = counts.get(matches[j].stem, 0) + 1
            if counts[matches[j].stem] == 1:
                distinct += 1
            j += 1
        if j == i:  # single match wider than the span (very long token); include it anyway
            counts[matches[i].stem] = counts.get(matches[i].stem, 0) + 1
            if counts[matches[i].stem] == 1:
                distinct += 1
            j = i + 1
        key = (distinct, j - i, -i)
        if best is None or key > best[0]:
            best = (key, i, j - 1)
        counts[matches[i].stem] -= 1
        if counts[matches[i].stem] == 0:
            distinct -= 1
    return best[1], best[2]


def words_before(text: str, pos: int, n: int, lo: int = 0) -> int:
    """Char offset of the start of the n-th word before ``pos`` (clamped to ``lo``)."""
    if n <= 0 or pos <= lo:
        return pos
    slice_start = max(lo, pos - n * CHARS_PER_WORD)
    starts = [m.start() for m in WORD_RE.finditer(text, slice_start, pos)]
    if len(starts) >= n:
        return starts[-n]
    return slice_start if slice_start > lo else lo


def words_after(text: str, pos: int, n: int, hi: Optional[int] = None) -> int:
    """Char offset of the end of the n-th word after ``pos`` (clamped to ``hi``)."""
    if hi is None:
        hi = len(text)
    if n <= 0 or pos >= hi:
        return pos
    slice_end = min(hi, pos + n * CHARS_PER_WORD)
    ends = [m.end() for m in WORD_RE.finditer(text, pos, slice_end)]
    if len(ends) >= n:
        return ends[n - 1]
    return slice_end


def _snap_outward(text: str, start: int, end: int, lo: int, hi: int) -> tuple[int, int]:
    """Move ``start`` back and ``end`` forward to word boundaries."""
    while start > lo and not text[start - 1].isspace():
        start -= 1
    while end < hi and not text[end].isspace():
        end += 1
    return start, end


_WS_RE = re.compile(r"\s+")


def _clean(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def _render(text: str, start: int, end: int, matches: Iterable[Match], highlight: bool) -> str:
    if not highlight:
        return _clean(text[start:end])
    out: List[str] = []
    pos = start
    for m in matches:
        if m.start < start or m.end > end:
            continue
        out.append(text[pos:m.start])
        out.append("<b>")
        out.append(text[m.start:m.end])
        out.append("</b>")
        pos = m.end
    out.append(text[pos:end])
    return _clean("".join(out))


def _decorate(snippet: str, start: int, end: int, text_len: int) -> str:
    if not snippet:
        return snippet
    if start > 0:
        snippet = ELLIPSIS + snippet
    if end < text_len:
        snippet = snippet + ELLIPSIS
    return snippet


def passage_score(matches: Sequence[Match], n_query_terms: int) -> float:
    """
    (distinct terms matched + density) / (query terms + 1), in 0..1.
    density = mean over matched terms of tf / (tf + 1), so repetition helps a
    little but one extra distinct term always helps more.
    """
    if not matches:
        return 0.0
    tf: Dict[str, int] = {}
    for m in matches:
        tf[m.stem] = tf.get(m.stem, 0) + 1
    density = sum(c / (c + 1) for c in tf.values()) / len(tf)
    return round((len(tf) + density) / (max(1, n_query_terms) + 1), 3)


def extract_passages(
    text: str,
    stems: Set[str],
    stemmer: "Stemmer.Stemmer",
    context_words: int = 20,
    max_passages: int = 5,
    highlight: bool = False,
    start_at: int = 0,
    end_at: Optional[int] = None,
) -> tuple[List[Passage], int]:
    """
    Up to ``max_passages`` non-overlapping passages of ``text`` for the query
    ``stems``, best first, plus the total number of query-term occurrences.

    Each passage is the densest remaining group of matches (see
    :func:`best_cluster`) with ``context_words`` words before its first and
    after its last matched word.

    ``start_at``/``end_at`` restrict the search to the body of the paper:
    ``start_at`` skips front matter (authors, affiliations) and ``end_at``
    stops before the reference list, where query terms often cluster densely
    but make poor snippets. See :func:`content_start` / :func:`content_end`.
    If nothing matches inside that range the whole text is searched.

    With no match anywhere (e.g. a title-only hit) the result is one passage
    of the first ``3 * context_words`` words of the body with score 0.
    """
    if not text:
        return [], 0
    text_len = len(text)
    start_at = max(0, min(start_at, text_len))
    hi = text_len if end_at is None else max(start_at, min(end_at, text_len))
    matches = find_matches(text, stems, stemmer, start_at, hi)
    if not matches and (start_at > 0 or hi < text_len):
        start_at, hi = 0, text_len
        matches = find_matches(text, stems, stemmer, 0)
    total_hits = len(matches)

    if not matches:
        end = words_after(text, start_at, 3 * context_words, hi)
        start, end = _snap_outward(text, start_at, end, start_at, hi)
        content = _decorate(_clean(text[start:end]), start, end, text_len)
        return ([Passage(content, 0.0, 0, start, end)] if content else []), 0

    n_terms = max(1, len(stems))
    passages: List[Passage] = []
    remaining = list(matches)
    while remaining and len(passages) < max_passages:
        i, j = best_cluster(remaining, CLUSTER_SPAN_CHARS)
        cluster = remaining[i:j + 1]
        start = words_before(text, cluster[0].start, context_words, start_at)
        end = words_after(text, cluster[-1].end, context_words, hi)
        start, end = _snap_outward(text, start, end, start_at, hi)
        # Score on every match inside the window, not only the seed cluster.
        inside = [m for m in matches if m.start >= start and m.end <= end]
        content = _decorate(_render(text, start, end, inside, highlight), start, end, text_len)
        passages.append(Passage(content, passage_score(inside, n_terms), len(inside), start, end))
        # Drop everything this window consumed so the next passage does not overlap.
        remaining = [m for m in remaining if m.end <= start or m.start >= end]

    passages.sort(key=lambda p: (-p.score, -p.hits, p.start))
    return passages, total_hits


def best_snippet(
    text: str,
    stems: Set[str],
    stemmer: "Stemmer.Stemmer",
    context_words: int = 20,
    highlight: bool = False,
    start_at: int = 0,
    end_at: Optional[int] = None,
) -> str:
    """The single best passage (see :func:`extract_passages`), or ``""``."""
    passages, _ = extract_passages(text, stems, stemmer, context_words, 1, highlight, start_at, end_at)
    return passages[0].content if passages else ""


# ---------------------------------------------------------------------------
# Helpers for S2ORC annotations
# ---------------------------------------------------------------------------

def _spans(annotations: Optional[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    if not annotations:
        return []
    raw = annotations.get(key)
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return []
    return [s for s in raw if isinstance(s, dict) and "start" in s]


def content_start(annotations: Optional[Dict[str, Any]], keys: Sequence[str] = ("abstract", "paragraph")) -> int:
    """
    Char offset where the body content begins: the earliest start of the
    first ``abstract``/``paragraph`` span. 0 when annotations are missing.
    Used to skip the author/affiliation block at the top of v2 texts.
    """
    starts = []
    for key in keys:
        spans = _spans(annotations, key)
        if spans:
            try:
                starts.append(int(spans[0]["start"]))
            except (TypeError, ValueError):
                pass
    return min(starts) if starts else 0


def content_end(annotations: Optional[Dict[str, Any]], text_len: int, keys: Sequence[str] = ("bibentry",)) -> int:
    """
    Char offset where the reference list begins (start of the first
    ``bibentry`` span), or ``text_len`` when there is none. Snippets are
    taken from before this point when possible.
    """
    starts = []
    for key in keys:
        for span in _spans(annotations, key):
            try:
                starts.append(int(span["start"]))
            except (TypeError, ValueError):
                pass
    end = min(starts) if starts else text_len
    return max(0, min(end, text_len))
