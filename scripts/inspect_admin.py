"""Run read-only GraphQL inspection as an existing privileged local operator."""

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from creditlens.errors import ServiceError
from creditlens.graphql_admin import execute_admin
from creditlens.ingestion_jobs import JobStore
from creditlens.runtime import open_workflow
from creditlens.settings import Settings
from creditlens.storage import GrantStore, open_database
from scripts.ingest_documents import bounded_read


def inspect(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    """Resolve existing SQL grants; this trusted CLI never creates an administrator identity."""
    if not settings.graphql_enabled:
        raise ValueError("Enable GraphQL inspection first")
    query = bounded_read(args.query, 10_000).decode("utf-8")
    variables = json.loads(bounded_read(args.variables, 16_384)) if args.variables else {}
    if not isinstance(variables, dict):
        raise ValueError("GraphQL variables must be an object")
    engine = open_database(settings.database_url)
    try:
        store = GrantStore(engine)
        actor = store.resolve(args.subject)
        if actor.role != "admin":
            raise ServiceError("access_denied", "Administrator access is required", 403)
        jobs = JobStore(engine, settings.ingestion_queue_id) if settings.ingestion_enabled else None
        with open_workflow(settings, store) as workflow:
            if workflow is None:
                raise ServiceError(
                    "workflow_unavailable", "Configured workflow is unavailable", 503
                )
            return execute_admin(query, variables, args.operation, workflow, actor, jobs)
    finally:
        engine.dispose()


def main() -> None:
    """Keep local query files and operator errors out of ordinary service logs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--query", type=Path, required=True)
    parser.add_argument("--variables", type=Path)
    parser.add_argument("--operation")
    args = parser.parse_args()
    try:
        result = inspect(args, Settings())
    except ServiceError as error:
        print(json.dumps({"error_code": error.code}))
        raise SystemExit(2) from None
    except SQLAlchemyError:
        print(json.dumps({"error_code": "storage_unavailable"}))
        raise SystemExit(2) from None
    except (ValueError, OSError):
        print(json.dumps({"error_code": "operator_input_invalid"}))
        raise SystemExit(2) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
