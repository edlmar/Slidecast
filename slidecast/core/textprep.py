"""Turn raw speaker notes into clean, chunked narration text.

Chatterbox paralinguistic tags such as ``[laugh]``, ``[chuckle]``, ``[cough]`` are preserved
verbatim.  Cleaning never splits a sentence; chunking never breaks mid-sentence unless a single
sentence exceeds the chunk budget, in which case it is split at clause boundaries, then words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from slidecast.core.config import TextSettings

# Bump when cleaning/chunking behaviour changes so cached audio is invalidated.
TEXTPREP_VERSION = "1"

_TAG_RE = re.compile(r"\[(?:laugh|chuckle|cough)\]", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-*•‣◦▪●○■]+|\d{1,3}[.)]|[a-zA-Z][.)]|>+)\s+", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?:[^)]+)\)")
_MD_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|`{1,3}|~~)(?=\S)(.+?)(?<=\S)\1")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"']+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])[\"'”’)]*\s+(?=[\"'“‘(\[A-Z0-9])")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[;:,—–])\s+")
# Sentence fragments ending in one of these are re-joined with the following fragment.
_NO_SPLIT_AFTER = re.compile(
    r"(?:^|\s)(?:e\.g|i\.e|vs|approx|Fig|Figs|Eq|Dr|Prof|Mr|Mrs|Ms|No|St|etc|cf|al|Jr|Sr)\.$"
)

_URL_TOKEN_WORDS = {
    ".": " dot ",
    "/": " slash ",
    "-": " dash ",
    "_": " underscore ",
    "?": " question mark ",
    "&": " and ",
    "=": " equals ",
    "#": " hash ",
    "@": " at ",
    "~": " tilde ",
    "%": " percent ",
    "+": " plus ",
    ":": " colon ",
}


def spoken_url(url: str) -> str:
    """Convert ``https://www.example.com/path?x=1`` into a speakable phrase."""
    u = url.strip().rstrip(".,;:!?")
    u = re.sub(r"^https?://", "", u, flags=re.IGNORECASE)
    u = re.sub(r"^www\.", "", u, flags=re.IGNORECASE)
    u = u.rstrip("/")
    out: list[str] = []
    for ch in u:
        out.append(_URL_TOKEN_WORDS.get(ch, ch))
    text = "".join(out)
    return _WS_RE.sub(" ", text).strip()


def _expand_abbreviations(text: str, table: dict[str, str]) -> str:
    if not table:
        return text
    # Longest keys first so "e.g." wins over "e".
    keys = sorted(table, key=len, reverse=True)
    for key in keys:
        # Word boundary that also works for keys ending in punctuation or containing '/'.
        pattern = r"(?<![\w/])" + re.escape(key) + r"(?![\w/])"
        text = re.sub(pattern, lambda _m, k=key: table[k], text)  # type: ignore[misc]
    return text


def clean_notes(raw: str, settings: TextSettings | None = None) -> str:
    """Normalise speaker notes into narration prose. Preserves paralinguistic tags."""
    settings = settings or TextSettings()
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\x0b", "\n").replace("\x0c", "\n")

    # Protect tags from every other regex by swapping them for placeholders.
    tags: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        tags.append(m.group(0).lower())
        return f"\x00{len(tags) - 1}\x00"

    text = _TAG_RE.sub(_stash, text)

    text = _MD_IMAGE_RE.sub(lambda m: m.group(1), text)
    text = _MD_LINK_RE.sub(lambda m: m.group(1), text)
    text = _MD_HEADING_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    for _ in range(2):  # nested emphasis, e.g. ***bold italic***
        text = _MD_EMPHASIS_RE.sub(lambda m: m.group(2), text)

    if settings.speak_urls:
        text = _EMAIL_RE.sub(lambda m: spoken_url(m.group(0)), text)
        text = _URL_RE.sub(lambda m: spoken_url(m.group(0)), text)
    else:
        text = _URL_RE.sub("", text)

    if settings.expand_abbreviations:
        text = _expand_abbreviations(text, settings.abbreviations)

    # Join lines: a line that does not end a sentence continues into the next one.
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.split("\n")]
    joined: list[str] = []
    for ln in lines:
        if not ln:
            continue
        if joined and not re.search(r"[.!?:;]$", joined[-1]):
            # Previous line had no terminal punctuation -> treat as a sentence break anyway
            # (slides notes are usually one thought per line).
            joined[-1] = joined[-1] + "."
        joined.append(ln)
    text = " ".join(joined)
    text = _WS_RE.sub(" ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:!?]){2,}", r"\1", text)
    if text and not re.search(r"[.!?]$", text) and not text.endswith("\x00"):
        text += "."

    def _restore(m: re.Match[str]) -> str:
        return tags[int(m.group(1))]

    text = re.sub(r"\x00(\d+)\x00", _restore, text)
    return text


def split_sentences(text: str) -> list[str]:
    """Split cleaned prose into sentences. Conservative: only splits after . ! ? + space + cap."""
    text = text.strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_END_RE.split(text) if p.strip()]
    merged: list[str] = []
    for p in parts:
        if merged and _NO_SPLIT_AFTER.search(merged[-1]):
            merged[-1] = f"{merged[-1]} {p}"
        else:
            merged.append(p)
    return merged


def _split_long_sentence(sentence: str, max_chars: int) -> list[str]:
    if len(sentence) <= max_chars:
        return [sentence]
    clauses = _CLAUSE_SPLIT_RE.split(sentence)
    pieces: list[str] = []
    for clause in clauses:
        if len(clause) <= max_chars:
            pieces.append(clause)
            continue
        words = clause.split(" ")
        cur = ""
        for w in words:
            candidate = f"{cur} {w}".strip()
            if len(candidate) > max_chars and cur:
                pieces.append(cur)
                cur = w
            else:
                cur = candidate
        if cur:
            pieces.append(cur)
    return _pack(pieces, max_chars)


def _pack(units: list[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for u in units:
        candidate = f"{cur} {u}".strip()
        if cur and len(candidate) > max_chars:
            chunks.append(cur)
            cur = u
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def chunk_text(text: str, max_chars: int = 250) -> list[str]:
    """Pack sentences into chunks of at most ``max_chars`` without breaking a sentence."""
    sentences = split_sentences(text)
    units: list[str] = []
    for s in sentences:
        units.extend(_split_long_sentence(s, max_chars))
    return _pack(units, max_chars)


def count_words(text: str) -> int:
    stripped = _TAG_RE.sub("", text)
    return len([w for w in stripped.split() if any(c.isalnum() for c in w)])


def estimate_duration(text: str, words_per_minute: int = 150) -> float:
    return count_words(text) / max(words_per_minute, 1) * 60.0


@dataclass(frozen=True)
class PreparedText:
    cleaned: str
    chunks: list[str]
    word_count: int
    estimated_seconds: float


def prepare(raw_notes: str, settings: TextSettings | None = None) -> PreparedText:
    settings = settings or TextSettings()
    cleaned = clean_notes(raw_notes, settings)
    chunks = chunk_text(cleaned, settings.chunk_chars)
    return PreparedText(
        cleaned=cleaned,
        chunks=chunks,
        word_count=count_words(cleaned),
        estimated_seconds=estimate_duration(cleaned, settings.words_per_minute),
    )
