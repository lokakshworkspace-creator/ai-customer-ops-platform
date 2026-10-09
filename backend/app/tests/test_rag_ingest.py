"""Ingest: dry-run plan, no-op for unchanged documents, atomic replacement, caps, guards, and the
command line. All with the fake embedder against the test database."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.policy.rules import get_rules
from app.rag.embedder import EmbedderUnavailable, FakeEmbedder
from app.rag.guards import KbDimensionMismatch
from app.rag.ingest import IngestCapExceeded, apply_plan, build_plan, content_hash
from app.rag.kb import KB_DIR, load_documents
from app.tests.conftest import CommittedDB, _test_db_url
from app.tests.policy_helpers import make_rules

RULES = get_rules()
DOCS = load_documents(RULES)
REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "kb_ingest.py"
BACKEND = REPO / "backend"


class Counting(FakeEmbedder):
    """A fake that counts calls and can fail on the Nth embed_documents call."""

    def __init__(self, dim: int = 768, fail_on_call: int | None = None) -> None:
        super().__init__(dim)
        self.fail_on_call = fail_on_call
        self.batches = 0

    async def embed_documents(self, docs):
        self.batches += 1
        if self.fail_on_call == self.batches:
            raise EmbedderUnavailable("rate limited")
        return await super().embed_documents(docs)


async def plan_for(session, docs=DOCS, model="fake-hash-v1", dim=768, ns="default"):
    return await build_plan(session, docs, namespace=ns, model=model, dim=dim)


async def snapshot(session: AsyncSession) -> dict:
    rows = (
        await session.execute(
            text(
                "SELECT d.source, d.version, d.content_hash, d.updated_at, "
                "array_agg(c.id ORDER BY c.ordinal) AS chunk_ids "
                "FROM kb_documents d LEFT JOIN kb_chunks c ON c.document_id = d.id "
                "GROUP BY d.id ORDER BY d.source"
            )
        )
    ).all()
    return {r.source: (r.version, r.content_hash, r.updated_at, r.chunk_ids) for r in rows}


async def count(session, table: str) -> int:
    return await session.scalar(text(f"SELECT count(*) FROM {table}"))


def doc_dir(tmp_path: Path, *names: str) -> Path:
    for n in names:
        shutil.copy(KB_DIR / n, tmp_path / n)
    return tmp_path


# --- the plan is read-only ----------------------------------------------------------------------


async def test_a_plan_on_an_empty_database_lists_every_document_as_new_and_writes_nothing(
    db_session,
) -> None:
    plan = await plan_for(db_session)
    assert {p.status for p in plan.docs} == {"new"} and len(plan.docs) == 10
    assert plan.orphans == [] and 20 <= sum(len(p.chunks) for p in plan.docs) <= 80
    assert plan.estimated_calls(8) == sum(-(-len(p.chunks) // 8) for p in plan.docs)
    assert (
        await count(db_session, "kb_documents") == 0 and await count(db_session, "kb_chunks") == 0
    )


# --- apply --------------------------------------------------------------------------------------


async def test_apply_writes_documents_and_chunks_with_vectors_audiences_and_models(
    db_session,
) -> None:
    emb = Counting()
    result = await apply_plan(db_session, await plan_for(db_session), emb)

    assert len(result.written) == 10 and result.unchanged == []
    assert await count(db_session, "kb_documents") == 10
    assert await count(db_session, "kb_chunks") == result.chunks_written
    audiences = dict(
        (
            await db_session.execute(text("SELECT audience, count(*) FROM kb_documents GROUP BY 1"))
        ).all()
    )
    assert audiences == {"customer": 8, "internal": 2}
    bad = await db_session.scalar(
        text(
            "SELECT count(*) FROM kb_chunks WHERE embedding_model <> 'fake-hash-v1' "
            "OR token_count < 1 OR tsv IS NULL OR vector_dims(embedding) <> 768 "
            "OR abs(vector_norm(embedding) - 1) > 1e-4"
        )
    )
    assert bad == 0  # every vector is 768 wide and unit length; the search vector was generated
    ordinals = await db_session.scalar(
        text(
            "SELECT bool_and(ord_ok) FROM (SELECT ordinal = row_number() OVER "
            "(PARTITION BY document_id ORDER BY ordinal) - 1 AS ord_ok FROM kb_chunks) t"
        )
    )
    assert ordinals is True
    headings = await db_session.scalar(
        text("SELECT count(*) FROM kb_chunks WHERE heading LIKE 'Returns policy > Electronics%'")
    )
    assert headings == 1
    assert emb.calls_made == emb.batches > 0


async def test_re_ingesting_unchanged_documents_is_a_no_op(db_session) -> None:
    await apply_plan(db_session, await plan_for(db_session), Counting())
    before = await snapshot(db_session)

    emb = Counting()
    plan = await plan_for(db_session)
    result = await apply_plan(db_session, plan, emb)

    assert {p.status for p in plan.docs} == {"unchanged"} and plan.to_embed == []
    assert result.written == [] and len(result.unchanged) == 10
    assert emb.calls_made == 0 and emb.batches == 0  # not one embedding call
    assert await snapshot(db_session) == before  # not one row, version or timestamp changed


async def test_only_the_changed_document_is_re_embedded_and_its_chunks_are_replaced(
    db_session, tmp_path
) -> None:
    names = ("returns-policy.md", "shipping.md", "warranty.md")
    d = doc_dir(tmp_path, *names)
    docs = load_documents(RULES, d)
    await apply_plan(db_session, await plan_for(db_session, docs), Counting())
    before = await snapshot(db_session)

    (d / "shipping.md").write_text(
        (d / "shipping.md").read_text(encoding="utf-8").replace("flat shipping fee", "small fee"),
        encoding="utf-8",
    )
    docs2 = load_documents(RULES, d)
    plan = await plan_for(db_session, docs2)
    assert {p.doc.source: p.status for p in plan.docs} == {
        "returns-policy.md": "unchanged",
        "shipping.md": "changed",
        "warranty.md": "unchanged",
    }
    emb = Counting()
    result = await apply_plan(db_session, plan, emb)

    after = await snapshot(db_session)
    assert result.written == ["shipping.md"] and emb.batches == 1
    assert after["shipping.md"][0] == 2 and before["shipping.md"][0] == 1  # version bumped
    assert set(after["shipping.md"][3]).isdisjoint(before["shipping.md"][3])  # new chunk rows
    assert after["returns-policy.md"] == before["returns-policy.md"]
    assert after["warranty.md"] == before["warranty.md"]
    assert "small fee" in await db_session.scalar(
        text(
            "SELECT string_agg(content, ' ') FROM kb_chunks c JOIN kb_documents d "
            "ON d.id = c.document_id WHERE d.source = 'shipping.md'"
        )
    )


async def test_a_changed_rule_value_re_embeds_the_documents_that_state_it(db_session) -> None:
    await apply_plan(db_session, await plan_for(db_session), Counting())
    rules = make_rules(**{"refund.max_order_age_days": 45})
    plan = await plan_for(db_session, load_documents(rules))
    changed = {p.doc.source for p in plan.docs if p.status == "changed"}
    assert {"returns-policy.md", "refunds.md", "warranty.md"} <= changed
    assert "payments.md" not in changed  # a document without that number is untouched


async def test_a_different_embedding_model_or_dimension_re_embeds_everything(db_session) -> None:
    await apply_plan(db_session, await plan_for(db_session), Counting())
    plan = await plan_for(db_session, model="another-model")
    assert {p.status for p in plan.docs} == {"changed"}
    assert content_hash(DOCS[0], "m", 768) != content_hash(DOCS[0], "m", 1536)


async def test_a_new_embedding_model_replaces_the_old_vectors_so_none_are_mixed(db_session) -> None:
    await apply_plan(db_session, await plan_for(db_session), Counting())

    class Other(Counting):
        def __init__(self) -> None:
            super().__init__()
            self.model = "other-model"

    await apply_plan(db_session, await plan_for(db_session, model="other-model"), Other())
    models = (
        (await db_session.execute(text("SELECT DISTINCT embedding_model FROM kb_chunks")))
        .scalars()
        .all()
    )
    assert models == ["other-model"]


# --- atomicity and failure ----------------------------------------------------------------------


async def test_when_embedding_fails_the_old_chunks_keep_serving_and_a_rerun_resumes(
    db_session, tmp_path
) -> None:
    d = doc_dir(tmp_path, "returns-policy.md", "shipping.md", "warranty.md")
    await apply_plan(db_session, await plan_for(db_session, load_documents(RULES, d)), Counting())
    before = await snapshot(db_session)
    for name in ("returns-policy.md", "shipping.md"):  # change two documents
        p = d / name
        p.write_text(p.read_text(encoding="utf-8") + "\nAn extra sentence.\n", encoding="utf-8")
    plan = await plan_for(db_session, load_documents(RULES, d))

    with pytest.raises(EmbedderUnavailable):
        await apply_plan(db_session, plan, Counting(fail_on_call=2))  # the 2nd document fails

    mid = await snapshot(db_session)
    changed_now = [s for s in mid if mid[s][0] == 2]
    assert len(changed_now) == 1  # the first finished and is committed ...
    stale = next(s for s in ("returns-policy.md", "shipping.md") if s not in changed_now)
    assert (
        mid[stale] == before[stale]
    )  # ... the failed one is exactly as it was (old chunks intact)

    result = await apply_plan(
        db_session, await plan_for(db_session, load_documents(RULES, d)), Counting()
    )
    assert result.written == [stale]  # resumed: only the unfinished document
    assert {v[0] for s, v in (await snapshot(db_session)).items() if s != "warranty.md"} == {2}


async def test_a_database_error_while_replacing_rolls_back_to_the_old_chunks(
    db_session, tmp_path, monkeypatch
) -> None:
    d = doc_dir(tmp_path, "shipping.md")
    await apply_plan(db_session, await plan_for(db_session, load_documents(RULES, d)), Counting())
    before = await snapshot(db_session)
    p = d / "shipping.md"
    p.write_text(p.read_text(encoding="utf-8") + "\nMore text.\n", encoding="utf-8")

    import app.rag.ingest as ingest

    real = ingest._INSERT_CHUNK

    class Boom:  # the second chunk insert fails AFTER the old chunks were deleted
        n = 0

        def __getattr__(self, name):
            return getattr(real, name)

    calls = {"n": 0}
    original_execute = db_session.execute

    async def flaky(stmt, params=None, *a, **kw):
        if stmt is real:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk full")
        return await original_execute(stmt, params, *a, **kw)

    monkeypatch.setattr(db_session, "execute", flaky)
    with pytest.raises(RuntimeError, match="disk full"):
        await apply_plan(
            db_session, await plan_for(db_session, load_documents(RULES, d)), Counting()
        )
    monkeypatch.undo()
    assert await snapshot(db_session) == before  # the delete and the first insert were rolled back


async def test_the_call_cap_stops_the_run_after_the_documents_that_fit(db_session) -> None:
    emb = Counting()
    plan = await plan_for(db_session)
    with pytest.raises(IngestCapExceeded, match=r"stopped after 2 embedding calls .* 2 document"):
        await apply_plan(db_session, plan, emb, max_calls=2)
    assert await count(db_session, "kb_documents") == 2  # the finished ones are committed
    result = await apply_plan(db_session, await plan_for(db_session), Counting())
    assert len(result.unchanged) == 2 and len(result.written) == 8  # a re-run continues


async def test_a_wrong_dimension_is_refused_before_any_embedding_call(db_session) -> None:
    emb = Counting(dim=512)
    plan = await plan_for(db_session, dim=512)
    with pytest.raises(KbDimensionMismatch, match=r"EMBEDDING_DIM=512 .* vector\(768\)"):
        await apply_plan(db_session, plan, emb)
    assert emb.calls_made == 0 and emb.batches == 0


async def test_orphans_are_only_deleted_with_prune(db_session, tmp_path) -> None:
    d = doc_dir(tmp_path, "returns-policy.md", "shipping.md")
    await apply_plan(db_session, await plan_for(db_session, load_documents(RULES, d)), Counting())
    (d / "shipping.md").unlink()
    plan = await plan_for(db_session, load_documents(RULES, d))
    assert plan.orphans == ["shipping.md"]
    await apply_plan(db_session, plan, Counting())
    assert await count(db_session, "kb_documents") == 2  # not deleted by default
    result = await apply_plan(db_session, plan, Counting(), prune=True)
    assert result.pruned == ["shipping.md"] and await count(db_session, "kb_documents") == 1
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM kb_chunks c JOIN kb_documents d "
                "ON d.id = c.document_id WHERE d.source = 'shipping.md'"
            )
        )
        == 0
    )


async def test_namespaces_are_independent_indexes(db_session) -> None:
    await apply_plan(db_session, await plan_for(db_session, ns="default"), Counting())
    plan = await plan_for(db_session, ns="demo")
    assert {p.status for p in plan.docs} == {"new"}  # nothing in "demo" yet
    await apply_plan(db_session, plan, Counting())
    assert await count(db_session, "kb_documents") == 20
    assert await count(db_session, "kb_documents WHERE namespace = 'demo'") == 10


# --- the command line, as a real process, against ops_test --------------------------------------


def run_cli(*args: str, **extra_env: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": _test_db_url().render_as_string(hide_password=False),
        "EMBEDDING_PROVIDER": "fake",
        "EMBEDDING_MODEL": "",
        "GEMINI_API_KEY": "",
        **extra_env,
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,  # a cold start costs ~11 s of imports
    )


@pytest.mark.usefixtures("committed_db")
async def test_the_command_line_is_a_dry_run_unless_told_otherwise(
    committed_db: CommittedDB,
) -> None:
    dry = run_cli()
    assert dry.returncode == 0, dry.stderr[-800:]
    assert "DRY RUN" in dry.stdout and "new" in dry.stdout and "FAKE embeddings" in dry.stdout
    assert "internal-agent-playbook.md" in dry.stdout and "model=fake-hash-v1" in dry.stdout
    assert "***" in dry.stdout and "_test" in dry.stdout  # the database, password masked
    async with committed_db.maker() as s:
        assert await s.scalar(text("SELECT count(*) FROM kb_documents")) == 0  # nothing written

    applied = run_cli("--apply")
    assert applied.returncode == 0, applied.stderr[-800:]
    assert "written  : 10 document(s)" in applied.stdout
    again = run_cli("--apply")
    assert "unchanged: 10" in again.stdout and "written  : 0 document(s)" in again.stdout
    async with committed_db.maker() as s:
        assert await s.scalar(text("SELECT count(*) FROM kb_documents")) == 10


@pytest.mark.usefixtures("committed_db")
async def test_the_command_line_refuses_a_bad_document_and_a_missing_key() -> None:
    bad = BACKEND / "app" / "data" / "kb_tmp_bad"
    try:
        bad.mkdir()
        (bad / "x.md").write_text(
            "---\ntitle: X\naudience: customer\n---\n# X\n\nWithin 45 days.\n", encoding="utf-8"
        )
        out = run_cli("--apply", "--dir", str(bad))
        assert out.returncode == 1 and "neither a" in out.stderr and "Traceback" not in out.stderr
    finally:
        shutil.rmtree(bad, ignore_errors=True)
    out = run_cli(EMBEDDING_PROVIDER="gemini", EMBEDDING_MODEL="m", GEMINI_API_KEY="")
    assert out.returncode == 1 and "GEMINI_API_KEY" in out.stderr and "Traceback" not in out.stderr
