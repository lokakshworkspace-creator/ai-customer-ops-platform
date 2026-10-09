"""Heading-aware chunking of a rendered markdown document.

Rules (all tested):

* A chunk never starts mid-section: the document is split on `#`, `##` and `###` headings, and each
  chunk remembers its heading PATH ("Returns policy > Electronics").
* A heading is never separated from its first paragraph: the first block of a section always
  goes into the section's first chunk.
* A bulleted list or a table stays with the paragraph that introduces it.
* Target about `target` estimated tokens per chunk. A section longer than that is split between
  blocks; a single over-long paragraph is split between sentences. When a section has to be split,
  the next chunk starts with a small overlap (about `overlap` tokens of the previous chunk's end).
* A tiny section (under `min_tokens`) is merged into its neighbour so no chunk is just a stub.

Token counts are estimates (words x 1.3): no tokenizer dependency.
"""

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,3})\s+(.*?)\s*#*\s*$")
_LIST_OR_TABLE = re.compile(r"^\s*([-*+]\s|\|)")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text.split()) * 1.3))


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    heading: str
    content: str
    token_count: int


@dataclass
class _Section:
    path: str
    blocks: list[str]


def _blocks(lines: list[str]) -> list[str]:
    """Paragraphs split on blank lines; a list or table block is glued to the paragraph before."""
    blocks: list[str] = []
    current: list[str] = []
    for line in [*lines, ""]:
        if line.strip():
            current.append(line.rstrip())
            continue
        if current:
            block = "\n".join(current)
            if blocks and _LIST_OR_TABLE.match(current[0]):
                blocks[-1] += "\n" + block
            else:
                blocks.append(block)
            current = []
    return blocks


def _sections(body: str) -> list[_Section]:
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    path = ""

    def flush() -> None:
        blocks = _blocks(buf)
        if blocks:
            sections.append(_Section(path, blocks))
        buf.clear()

    for line in body.splitlines():
        m = _HEADING.match(line)
        if not m:
            buf.append(line)
            continue
        flush()
        level, title = len(m.group(1)), m.group(2)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " > ".join(t for _, t in stack)
    flush()
    return sections


def _split_long(block: str, target: int) -> list[str]:
    """One block larger than the target: split between sentences."""
    pieces: list[str] = []
    current: list[str] = []
    for sentence in _SENTENCE.split(block):
        for part in _split_words(sentence, target):  # a "sentence" can itself be too long
            if current and estimate_tokens(" ".join(current + [part])) > target:
                pieces.append(" ".join(current))
                current = []
            current.append(part)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _split_words(text: str, target: int) -> list[str]:
    """Last resort for text with no sentence breaks: cut between words."""
    if estimate_tokens(text) <= target:
        return [text]
    words = text.split()
    per = max(1, int(target / 1.3))
    return [" ".join(words[i : i + per]) for i in range(0, len(words), per)]


def _tail(text: str, overlap: int) -> str:
    """About `overlap` tokens from the end of `text`, from a sentence boundary if possible."""
    sentences = _SENTENCE.split(text.strip())
    out: list[str] = []
    for s in reversed(sentences):
        if out and estimate_tokens(" ".join([s, *out])) > overlap:
            break
        out.insert(0, s)
    return " ".join(out) if estimate_tokens(" ".join(out)) <= overlap * 2 else ""


def chunk_markdown(
    body: str, *, target: int = 300, overlap: int = 40, min_tokens: int = 40
) -> list[Chunk]:
    raw: list[tuple[str, str]] = []  # (heading path, content)
    for section in _sections(body):
        units: list[str] = []
        for block in section.blocks:
            units += _split_long(block, target) if estimate_tokens(block) > target else [block]
        pieces: list[str] = []
        current: list[str] = []
        for unit in units:
            if current and estimate_tokens("\n\n".join(current + [unit])) > target:
                pieces.append("\n\n".join(current))
                carry = _tail(current[-1], overlap)
                current = [carry] if carry else []
            current.append(unit)
        if current:
            pieces.append("\n\n".join(current))
        raw += [(section.path, p) for p in pieces]

    merged: list[tuple[str, str]] = []
    for heading, content in raw:
        if merged and estimate_tokens(content) < min_tokens:
            prev_h, prev_c = merged[-1]
            label = f"{heading.split(' > ')[-1]}: " if heading != prev_h and heading else ""
            if estimate_tokens(prev_c + content) <= int(target * 1.5):
                merged[-1] = (prev_h, f"{prev_c}\n\n{label}{content}")
                continue
        merged.append((heading, content))
    if len(merged) > 1 and estimate_tokens(merged[0][1]) < min_tokens:
        (h0, c0), (h1, c1) = merged[0], merged[1]
        merged[1] = (
            (h1, f"{c0}\n\n{c1}") if h0 == h1 else (h0, f"{c0}\n\n{h1.split(' > ')[-1]}: {c1}")
        )
        merged.pop(0)

    return [Chunk(i, h, c, estimate_tokens(c)) for i, (h, c) in enumerate(merged)]
