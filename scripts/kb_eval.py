"""Measure retrieval quality on the golden questions (backend/app/data/kb_eval/golden.yaml).

    backend/.venv/Scripts/python scripts/kb_eval.py --namespace eval_g001

Read-only: it embeds each question (about 30 embedding calls, paced by the embedder) and queries
the index. Index first with the same embedder:  scripts/kb_ingest.py --apply --namespace NAME.

Which embedder is used comes from .env (EMBEDDING_PROVIDER / EMBEDDING_MODEL / GEMINI_API_KEY); the
key is never printed. With the fake embedder the numbers only show that the plumbing works: they
say nothing about real retrieval quality, so do not compare modes from a fake run.
"""

import argparse
import asyncio
import sys

from app.core.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.policy.rules import get_rules
from app.rag.embedder import EmbedderError
from app.rag.eval import evaluate, format_report, load_golden
from app.rag.factory import build_embedder
from app.rag.guards import KbConfigError
from app.rag.kb import KbError, load_documents
from app.rag.retriever import ensure_ready
from app.tools.handlers.knowledge import retrieval_params


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    namespace = args.namespace or settings.kb_namespace
    try:
        docs = load_documents(get_rules())
        embedder = build_embedder(settings)
        golden = load_golden()
    except (KbError, EmbedderError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    params = retrieval_params(embedder)
    print(f"embedder : {embedder.name}  model={embedder.model}  dim={embedder.dim}")
    if embedder.name == "fake":
        print("           (FAKE embeddings: these numbers say nothing about real quality)")
    engine = get_engine()
    try:
        async with get_sessionmaker()() as session:
            await ensure_ready(session, dim=embedder.dim, namespace=namespace, model=embedder.model)
            report = await evaluate(
                session, embedder, docs, golden, namespace=namespace, params=params
            )
            await session.rollback()
    except (KbConfigError, EmbedderError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print()
    print(format_report(report, params))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--namespace", help="index to evaluate (default: KB_NAMESPACE)")
    sys.exit(asyncio.run(main(ap.parse_args())))
