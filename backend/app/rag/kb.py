"""The knowledge base on disk: markdown files with a YAML front matter.

Documents INFORM answers; they never AUTHORIZE actions (golden rule 3). rules.yaml and the tool
executor decide everything. Where a document states a business number (the return window, the
automatic refund limit, ...) it does so with a PLACEHOLDER such as ``{{refund.max_order_age_days}}``
that is rendered from rules.yaml when the document is loaded, so it cannot contradict the rules.

A consistency check makes that hold for the rest of the text too: every number in a document (as
digits OR as a number word) must either come from a placeholder or be listed in the front matter
under `other_numbers` (facts the rules do not cover, such as delivery times). A literal "within 45
days" or "500" therefore fails loading, and ingest refuses to index such a document.

Front matter::

    ---
    title: Returns policy
    audience: customer          # customer | internal
    other_numbers: ["3", "5"]   # numbers the text may state that are not rule values
    ---
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from app.db.models.enums import KbAudience
from app.policy.rules import Rules

KB_DIR = Path(__file__).resolve().parents[1] / "data" / "kb"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+\.[a-z_]+)\s*\}\}")
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
# "one" is left out on purpose: mostly a pronoun ("each one"); a digit "1" is still caught.
_NUMBER_WORDS = re.compile(
    r"\b(zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|thousand|dozen)\b",
    re.I,
)


class KbError(Exception):
    """A knowledge-base document is invalid (bad front matter, unknown placeholder, ...)."""


class KbConsistencyError(KbError):
    """A document states a number that does not come from rules.yaml and is not declared."""


@dataclass(frozen=True, slots=True)
class KbDoc:
    source: str  # the file name, unique within a namespace
    title: str
    audience: KbAudience
    body: str  # RENDERED markdown (placeholders replaced)
    other_numbers: tuple[str, ...]


def _words(values: Any) -> str:
    return ", ".join(sorted(str(v).replace("_", " ") for v in values))


def rule_values(rules: Rules) -> dict[str, str]:
    """The only values a document may pull from rules.yaml, as display text."""
    r = rules.refund
    return {
        "refund.currency": r.currency,
        "refund.auto_approve_max": format(Decimal(r.auto_approve_max), "f"),
        "refund.max_order_age_days": str(r.max_order_age_days),
        "refund.velocity_window_days": str(r.velocity_window_days),
        "refund.velocity_max_refunds": str(r.velocity_max_refunds),
        "refund.refundable_statuses": _words(s.value for s in r.refundable_statuses),
        "refund.auto_approve_reasons": _words(r.auto_approve_reasons),
        "refund.review_reasons": _words(r.valid_reasons - r.auto_approve_reasons),
        "refund.valid_reasons": _words(r.valid_reasons),
        "cancel.cancellable_statuses": _words(s.value for s in rules.cancel.cancellable_statuses),
        "cancel.refund_required_statuses": _words(
            s.value for s in rules.cancel.refund_required_statuses
        ),
    }


def render(template: str, values: dict[str, str], where: str = "") -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            raise KbError(f"{where}: unknown placeholder {{{{{key}}}}} (known: {sorted(values)})")
        return values[key]

    return _PLACEHOLDER.sub(sub, template)


def numbers_in(text: str) -> list[str]:
    """Every number-like token: digit runs, and number words."""
    return _DIGITS.findall(text) + [w.lower() for w in _NUMBER_WORDS.findall(text)]


def check_numbers(source: str, template: str, other_numbers: tuple[str, ...]) -> None:
    """Numbers in the TEMPLATE (placeholders are not expanded yet) must be declared."""
    declared = {n.lower() for n in other_numbers}
    stray = [n for n in numbers_in(template) if n.lower() not in declared]
    if stray:
        raise KbConsistencyError(
            f"{source}: states number(s) {sorted(set(stray))} that are neither a "
            "{{placeholder}} from rules.yaml nor declared under other_numbers in the front matter"
        )


def parse_document(source: str, raw: str, rules: Rules) -> KbDoc:
    raw = raw.replace("\r\n", "\n")
    m = _FRONT_MATTER.match(raw)
    if not m:
        raise KbError(f"{source}: missing front matter (--- ... ---)")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise KbError(f"{source}: invalid front matter: {exc}") from None
    try:
        audience = KbAudience(meta["audience"])
        title = str(meta["title"]).strip()
    except (KeyError, ValueError):
        raise KbError(
            f"{source}: front matter needs title and audience (customer|internal)"
        ) from None
    if not title:
        raise KbError(f"{source}: empty title")
    other = tuple(str(n) for n in meta.get("other_numbers") or [])
    template = raw[m.end() :].strip() + "\n"
    check_numbers(source, template, other)
    return KbDoc(source, title, audience, render(template, rule_values(rules), source), other)


def load_documents(rules: Rules, directory: Path = KB_DIR) -> list[KbDoc]:
    docs = [
        parse_document(p.name, p.read_text(encoding="utf-8"), rules)
        for p in sorted(directory.glob("*.md"))
    ]
    if len({d.source for d in docs}) != len(docs):  # pragma: no cover - file names are unique
        raise KbError("duplicate document source")
    return docs
