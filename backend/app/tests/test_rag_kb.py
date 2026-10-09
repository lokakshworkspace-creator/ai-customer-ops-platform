"""The sample knowledge base: documents render from rules.yaml, cannot contradict it, and chunk
sensibly. No database, no network."""

import re

import pytest

from app.db.models.enums import KbAudience
from app.policy.rules import get_rules
from app.rag.chunker import chunk_markdown, estimate_tokens
from app.rag.kb import (
    KB_DIR,
    KbConsistencyError,
    KbError,
    load_documents,
    numbers_in,
    parse_document,
    rule_values,
)
from app.tests.policy_helpers import make_rules

RULES = get_rules()
DOCS = load_documents(RULES)
BY_SOURCE = {d.source: d for d in DOCS}


def doc(body: str, other: str = "[]", audience: str = "customer", title: str = "T") -> str:
    return f"---\ntitle: {title}\naudience: {audience}\nother_numbers: {other}\n---\n{body}\n"


# --- the sample knowledge base ------------------------------------------------------------------


def test_there_are_ten_documents_two_of_them_internal() -> None:
    assert len(DOCS) == 10
    internal = [d for d in DOCS if d.audience == KbAudience.internal]
    assert len(internal) == 2 and all(d.source.startswith("internal-") for d in internal)
    assert len({d.title for d in DOCS}) == 10 and len({d.source for d in DOCS}) == 10


def test_every_document_renders_completely() -> None:
    for d in DOCS:
        assert "{{" not in d.body and "}}" not in d.body, d.source
        assert d.body.startswith("#"), d.source


def test_rule_numbers_in_the_documents_are_the_rules_numbers() -> None:
    refunds = BY_SOURCE["refunds.md"].body
    r = RULES.refund
    assert f"{r.currency} {r.auto_approve_max}" in refunds  # 500.00
    assert (
        f"more than {r.velocity_max_refunds} refunds within {r.velocity_window_days} days"
        in refunds
    )
    assert f"older than {r.max_order_age_days} days" in refunds
    assert (
        "damaged, defective, late delivery, not as described, not delivered, wrong item" in refunds
    )
    assert "changed mind, order cancelled, other" in refunds
    assert (
        "pending" in BY_SOURCE["cancellations.md"].body
        and "paid" in BY_SOURCE["cancellations.md"].body
    )


def test_the_documents_follow_the_rules_when_the_rules_change() -> None:
    rules = make_rules(**{"refund.max_order_age_days": 45, "refund.auto_approve_max": "750.00"})
    changed = {d.source: d for d in load_documents(rules)}
    assert "up to 45 days ago" in changed["returns-policy.md"].body
    assert "750.00" in changed["refunds.md"].body and "500.00" not in changed["refunds.md"].body
    assert "30" not in changed["returns-policy.md"].body


def test_customer_documents_never_contain_internal_material() -> None:
    for d in DOCS:
        if d.audience == KbAudience.customer:
            assert not re.search(r"AMBER-HOLD|RED-STOP|INTERNAL ONLY|playbook", d.body, re.I), (
                d.source
            )


def test_every_number_in_every_template_comes_from_a_rule_or_is_declared() -> None:
    """The consistency check, applied to the real files (parse_document raises otherwise)."""
    for path in sorted(KB_DIR.glob("*.md")):
        parse_document(path.name, path.read_text(encoding="utf-8"), RULES)


# --- the check really catches a contradiction (negative controls) -------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "# R\n\nItems can be returned within 45 days.",  # contradicts the 30-day rule
        "# R\n\nItems can be returned within thirty days.",  # a number WORD
        "# R\n\nRefunds up to 500 are paid at once.",
        "# R\n\nYou may ask for 3 refunds.",
        "# R\n\nSee section 2.5 for details.",
    ],
)
def test_a_literal_number_that_is_not_declared_fails(body: str) -> None:
    with pytest.raises(KbConsistencyError, match="neither a"):
        parse_document("x.md", doc(body), RULES)


def test_declared_numbers_pass_and_placeholders_do_not_need_declaring() -> None:
    ok = parse_document(
        "x.md",
        doc(
            "# R\n\nDelivery takes 3 to 5 days; returns take {{refund.max_order_age_days}} days.",
            '["3", "5"]',
        ),
        RULES,
    )
    assert "3 to 5 days" in ok.body and "30 days" in ok.body


def test_a_number_written_in_the_front_matter_list_is_the_only_exception() -> None:
    with pytest.raises(KbConsistencyError):
        parse_document("x.md", doc("# R\n\nTakes 4 days.", '["3"]'), RULES)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("# no front matter\n", "missing front matter"),
        (doc("# R\n\nText {{refund.nope}} here."), "unknown placeholder"),
        (doc("# R\n\nText.", audience="public"), "title and audience"),
        ("---\ntitle: ''\naudience: customer\n---\n# R\n", "empty title"),
        ("---\n: [bad\n---\n# R\n", "invalid front matter"),
    ],
)
def test_malformed_documents_are_refused_with_a_clear_message(raw: str, message: str) -> None:
    with pytest.raises(KbError, match=message):
        parse_document("x.md", raw, RULES)


def test_numbers_are_found_as_digits_and_as_words() -> None:
    assert numbers_in("30 days, 3.5 kg and five items, twelve months") == [
        "30",
        "3.5",
        "five",
        "twelve",
    ]


def test_the_rule_values_cover_what_the_documents_use() -> None:
    assert {
        "refund.max_order_age_days",
        "refund.auto_approve_max",
        "cancel.cancellable_statuses",
    } <= set(rule_values(RULES))


# --- chunking -----------------------------------------------------------------------------------


def words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n)) + "."


def test_each_section_becomes_a_chunk_with_its_heading_path() -> None:
    body = (
        f"# Returns\n\n## Time limit\n\n{words(60, 'a')}\n\n"
        f"## Electronics\n\n### Opened items\n\n{words(60, 'b')}\n"
    )
    chunks = chunk_markdown(body)
    assert [c.heading for c in chunks] == [
        "Returns > Time limit",
        "Returns > Electronics > Opened items",
    ]
    assert [c.ordinal for c in chunks] == [0, 1]


def test_a_heading_is_never_split_from_its_first_paragraph() -> None:
    body = f"# Doc\n\n## First\n\n{words(500, 'x')}\n\n## Second\n\n{words(60, 'y')}\n"
    chunks = chunk_markdown(body)
    first = [c for c in chunks if c.heading == "Doc > First"]
    assert len(first) >= 2  # the long section was split ...
    assert first[0].content.startswith("x0 x1")  # ... but its first paragraph opens its first chunk
    assert all(c.content.strip() for c in chunks)  # no chunk is just a heading


def test_a_list_stays_with_its_lead_in_paragraph() -> None:
    lead = "These reasons are accepted:"
    body = (
        f"# D\n\n## Reasons\n\n{words(30, 'p')}\n\n{lead}\n\n- damaged\n- defective\n- wrong item\n"
    )
    (chunk,) = chunk_markdown(body, min_tokens=5)
    assert f"{lead}\n- damaged\n- defective\n- wrong item" in chunk.content.replace("\n\n", "\n")


def test_long_sections_split_with_a_small_overlap_and_stay_near_the_target() -> None:
    sentences = [
        f"Sentence number {chr(97 + i % 26)}{i} says something useful about shipping."
        for i in range(80)
    ]
    body = "# D\n\n## Long\n\n" + " ".join(sentences) + "\n"
    chunks = chunk_markdown(body, target=120, overlap=30)
    assert len(chunks) >= 4
    assert all(c.token_count <= 120 * 1.5 for c in chunks)
    # overlap: the start of each later chunk repeats the end of the one before
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        last_sentence = prev.content.split(". ")[-1].strip().rstrip(".")
        assert last_sentence in nxt.content[: len(nxt.content) // 2 + 120]
    assert "Sentence number a0" in chunks[0].content and "a52" in "".join(c.content for c in chunks)


def test_tiny_sections_are_merged_into_a_neighbour() -> None:
    body = f"# D\n\n## Big\n\n{words(120, 'm')}\n\n## Tiny\n\nOne short line.\n"
    chunks = chunk_markdown(body)
    assert len(chunks) == 1 and "Tiny: One short line." in chunks[0].content
    body = f"# D\n\n## Tiny\n\nOne short line.\n\n## Big\n\n{words(120, 'm')}\n"
    assert len(chunk_markdown(body)) == 1


def test_chunking_is_deterministic_and_handles_empty_and_headless_text() -> None:
    body = f"# D\n\n## A\n\n{words(80, 'q')}\n"
    assert chunk_markdown(body) == chunk_markdown(body)
    assert chunk_markdown("") == [] and chunk_markdown("# Only a heading\n") == []
    (c,) = chunk_markdown(
        "Just a paragraph with no heading at all, but long enough to count.", min_tokens=1
    )
    assert c.heading == "" and c.token_count > 0


def test_the_real_documents_chunk_into_a_sensible_number_of_sensible_chunks() -> None:
    total = 0
    for d in DOCS:
        chunks = chunk_markdown(d.body)
        assert chunks, d.source
        assert all(1 <= c.token_count <= 450 for c in chunks), d.source
        assert all(c.content.strip() and c.heading for c in chunks), d.source
        total += len(chunks)
        # no content is lost: every paragraph of the document is in some chunk
        joined = " ".join(c.content for c in chunks).replace("\n", " ")
        for paragraph in re.split(r"\n\s*\n", d.body):
            if paragraph.lstrip().startswith("#") and "\n" not in paragraph.strip():
                continue
            text = " ".join(paragraph.split())
            assert text.split(". ")[0][:40] in " ".join(joined.split()), (d.source, text[:50])
    assert 20 <= total <= 80, total


def test_token_estimates_are_stable() -> None:
    assert estimate_tokens("one two three") == 4 and estimate_tokens("") == 1
