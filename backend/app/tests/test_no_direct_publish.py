"""Golden rule 7, enforced structurally: nothing but the relay talks to Kafka, and every event is
created through `add_event` inside the caller's transaction.

This scans the source of the application (not the tests). If a request handler, service or tool
ever imports a Kafka client or the producer wrapper, or builds an OutboxEvent by hand, a test here
fails.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1]  # backend/app

# Who may import what. Everything else must not.
MAY_IMPORT_AIOKAFKA = {"events/kafka.py", "events/consumers/base.py"}
MAY_IMPORT_KAFKA_WRAPPER = {  # app.events.kafka
    "events/relay.py",
    "events/consumers/base.py",
    "events/consumers/notifications.py",
}
MAY_CONSTRUCT_OUTBOX_EVENT = {"events/outbox.py"}
MAY_CALL_ADD_EVENT = {  # events/outbox.py only DEFINES add_event
    "modules/refunds/service.py",
    "modules/tickets/service.py",
}


def source_files() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel.startswith("tests/") or "/__pycache__/" in rel:
            continue
        out.append((rel, ast.parse(path.read_text(encoding="utf-8"))))
    return out


def imported_modules(tree: ast.Module) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            # `from app.events import kafka` imports the module app.events.kafka
            mods |= {f"{node.module}.{a.name}" for a in node.names}
    return mods


def called_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(
                f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            )
    return names


def test_only_the_event_modules_import_a_kafka_client() -> None:
    offenders = {
        rel
        for rel, tree in source_files()
        if any(
            m == "aiokafka"
            or m.startswith("aiokafka.")
            or m.split(".")[0] in {"kafka", "confluent_kafka"}
            for m in imported_modules(tree)
        )
    }
    assert offenders == MAY_IMPORT_AIOKAFKA, offenders ^ MAY_IMPORT_AIOKAFKA


def test_only_the_relay_and_consumers_import_the_producer_wrapper() -> None:
    wrapper = "app.events.kafka"
    offenders = {rel for rel, tree in source_files() if wrapper in imported_modules(tree)}
    assert offenders == MAY_IMPORT_KAFKA_WRAPPER, offenders ^ MAY_IMPORT_KAFKA_WRAPPER


def test_request_paths_cannot_reach_the_relay_or_consumers() -> None:
    """The HTTP layer, services, tools and policy never import the relay / consumer modules."""
    request_side = ("api/", "modules/", "tools/", "policy/", "core/", "db/")
    banned = ("app.events.relay", "app.events.consumers", "app.events.kafka", "aiokafka")
    for rel, tree in source_files():
        if not rel.startswith(request_side):
            continue
        bad = {m for m in imported_modules(tree) if m.startswith(banned)}
        assert not bad, f"{rel} imports {sorted(bad)}: request paths must only write the outbox"


def test_no_one_calls_a_producer_outside_the_event_modules() -> None:
    for rel, tree in source_files():
        if rel.startswith("events/"):
            continue
        calls = called_names(tree)
        assert not calls & {"send_and_wait", "AIOKafkaProducer", "KafkaEventProducer", "publish"}, (
            rel
        )


def test_outbox_rows_are_only_ever_built_by_add_event() -> None:
    constructors = {rel for rel, tree in source_files() if "OutboxEvent" in called_names(tree)}
    assert constructors == MAY_CONSTRUCT_OUTBOX_EVENT, constructors


def test_services_that_emit_events_go_through_add_event() -> None:
    callers = {rel for rel, tree in source_files() if "add_event" in called_names(tree)}
    assert callers == MAY_CALL_ADD_EVENT, callers ^ MAY_CALL_ADD_EVENT


def test_add_event_never_commits_or_flushes() -> None:
    tree = ast.parse((APP / "events/outbox.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "add_event")
    assert not called_names(ast.Module(body=fn.body, type_ignores=[])) & {
        "commit",
        "flush",
        "begin",
    }


def test_the_application_has_no_other_way_to_write_the_outbox_table() -> None:
    """No raw SQL against outbox_events outside the relay, its admin helpers and the migrations."""
    allowed = {"events/relay.py", "events/admin.py", "db/models/outbox.py"}
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel.startswith(("tests/", "db/migrations/")) or rel in allowed:
            continue
        assert "outbox_events" not in path.read_text(encoding="utf-8"), rel
