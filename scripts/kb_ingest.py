"""Index the knowledge base (backend/app/data/kb/*.md) into Postgres.

    backend/.venv/Scripts/python scripts/kb_ingest.py            # DRY RUN (default): no API calls, no writes
    backend/.venv/Scripts/python scripts/kb_ingest.py --apply    # embed and write
    ... --prune          with --apply: also delete documents that are no longer on disk
    ... --namespace NAME the index to write (default: KB_NAMESPACE, "default")
    ... --max-calls N    stop after N embedding API calls (default 200)

Which embedder is used comes from .env: EMBEDDING_PROVIDER=fake (the default: deterministic
hash vectors, meaningless for real search) or gemini (EMBEDDING_MODEL and GEMINI_API_KEY). Switching
the model re-embeds every document (vectors of different models are not comparable). The script
never prints the API key. It only touches kb_documents and kb_chunks.

An unchanged document is skipped (nothing embedded, nothing written); a changed one has its chunks
replaced in a single transaction; an interrupted run is simply resumed by running it again.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.core.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.policy.rules import get_rules
from app.rag.embedder import EmbedderError
from app.rag.factory import build_embedder
from app.rag.guards import KbConfigError, check_dimension
from app.rag.ingest import IngestCapExceeded, apply_plan, build_plan
from app.rag.kb import KB_DIR, KbError, load_documents


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    namespace = args.namespace or settings.kb_namespace
    try:
        docs = load_documents(get_rules(), Path(args.dir))
        embedder = build_embedder(settings)
    except (KbError, EmbedderError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    engine = get_engine()
    print(f"database : {engine.url.render_as_string(hide_password=True)}")
    print(f"namespace: {namespace}")
    print(f"embedder : {embedder.name}  model={embedder.model}  dim={embedder.dim}")
    if embedder.name == "fake":
        print(
            "           (FAKE embeddings: for tests and demos only; they are meaningless for real search)"
        )
    try:
        async with get_sessionmaker()() as session:
            await check_dimension(session, embedder.dim)
            plan = await build_plan(
                session, docs, namespace=namespace, model=embedder.model, dim=embedder.dim
            )
            print(f"\n{'document':<42}{'audience':<10}{'chunks':>7}  status")
            for p in plan.docs:
                print(
                    f"{p.doc.source:<42}{p.doc.audience.value:<10}{len(p.chunks):>7}  {p.status}"
                    + (f" (version {p.version})" if p.status != "unchanged" else "")
                )
            for source in plan.orphans:
                print(
                    f"{source:<42}{'':<10}{'':>7}  orphan (not on disk)"
                    + (
                        " -> will be deleted"
                        if args.prune and args.apply
                        else " (use --prune to delete)"
                    )
                )
            calls = plan.estimated_calls(settings.embedding_batch_size)
            print(
                f"\nto embed: {len(plan.to_embed)} document(s), "
                f"{sum(len(p.chunks) for p in plan.to_embed)} chunk(s), about {calls} API call(s) "
                f"(batches of {settings.embedding_batch_size})"
            )
            if not args.apply:
                print("DRY RUN: nothing was embedded and nothing was written. Re-run with --apply.")
                return 0
            result = await apply_plan(
                session, plan, embedder, max_calls=args.max_calls, prune=args.prune
            )
    except IngestCapExceeded as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 3
    except (KbConfigError, EmbedderError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(
        f"\nwritten  : {len(result.written)} document(s), {result.chunks_written} chunk(s), "
        f"{result.calls} API call(s)"
    )
    print(f"unchanged: {len(result.unchanged)}   pruned: {len(result.pruned)}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="embed and write (default is a dry run)")
    ap.add_argument(
        "--prune", action="store_true", help="with --apply: delete documents not on disk"
    )
    ap.add_argument("--namespace", help="index name (default: KB_NAMESPACE)")
    ap.add_argument("--max-calls", type=int, default=200, help="cap on embedding API calls")
    ap.add_argument("--dir", default=str(KB_DIR), help="directory of markdown documents")
    sys.exit(asyncio.run(main(ap.parse_args())))
