"""Who may read which knowledge-base documents: THE one place that maps a role to audiences.

The audience is derived from the AUTHENTICATED role, in code. It is never a request parameter and
never anything a model says. The retriever then enforces it inside the SQL.

* The staff lookup (`GET /api/v1/admin/knowledge/search`) uses `audiences_for_role(role)`: an
  administrator sees customer and internal documents, everyone else sees customer documents only.
* The assistant's `search_knowledge_base` tool does NOT use this function: it is hard-wired to
  customer documents for every caller, an administrator included (tools/handlers/knowledge.py).
"""

from app.db.models.enums import KbAudience, Role


def audiences_for_role(role: Role) -> tuple[KbAudience, ...]:
    if role == Role.admin:
        return (KbAudience.customer, KbAudience.internal)
    return (KbAudience.customer,)
