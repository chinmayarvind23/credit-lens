"""Run the real lexical workflow over public synthetic evidence inside a browser worker."""

import json
from pathlib import Path

from creditlens.domain import Page, QueryRequest
from creditlens.errors import ServiceError
from creditlens.retrieval import EvidenceCatalog
from creditlens.storage import GrantStore, open_database
from creditlens.workflow import QueryWorkflow


class BrowserDemo:
    """Browser scope is a demonstration, never a boundary for confidential evidence."""

    def __init__(self, fixture: dict) -> None:
        """Each worker owns an ephemeral SQLite grant/audit store and public fixture catalog."""
        self.store = GrantStore(open_database("sqlite:///:memory:"))
        self.store.seed_demo()
        self.borrowers = fixture["borrowers"]
        self.catalog = EvidenceCatalog(tuple(Page.model_validate(p) for p in fixture["pages"]))
        self.workflow = QueryWorkflow(self.catalog, self.store)

    def dispatch(self, operation: str, payload: dict) -> dict:
        """Accept structured requests only; question text is data and never executed as Python."""
        principal = self.store.resolve("synthetic-demo")
        if operation == "borrowers":
            return {"mode": "demo", "borrowers": self.borrowers}
        request = QueryRequest.model_validate(payload["request"])
        if operation == "query":
            return self.workflow.query(request, principal).model_dump(mode="json")
        if operation == "evidence":
            chunks, _ = self.catalog.snapshot(principal, request.borrower_id, request.effective_at)
            for chunk in chunks:
                if chunk.chunk_id == payload["chunk_id"]:
                    return chunk.model_dump(mode="json")
            raise ServiceError("evidence_not_found", "Evidence is unavailable", 404)
        raise ValueError("Unknown browser operation")

    def dispatch_json(self, operation: str, payload: dict) -> str:
        """Preserve JSON null across Pyodide; direct toJs maps Python None to undefined."""
        return json.dumps(self.dispatch(operation, payload))


def initialize() -> BrowserDemo:
    """Read only the build-generated public fixture from the worker's virtual filesystem."""
    return BrowserDemo(json.loads(Path("/app/fixture.json").read_text()))
