"""Bounded read-only GraphQL inspection reuses current grants and canonical authority."""

from datetime import date
from typing import Any
from uuid import UUID

from graphql import (
    GraphQLError,
    GraphQLResolveInfo,
    OperationType,
    build_schema,
    execute_sync,
    get_operation_ast,
    parse,
    specified_rules,
    validate,
)
from graphql.language.ast import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    SelectionSetNode,
)
from graphql.validation import NoSchemaIntrospectionCustomRule
from sqlalchemy import select

from creditlens.access import authorize_borrower
from creditlens.domain import Principal
from creditlens.errors import ServiceError
from creditlens.ingestion_jobs import JobStore
from creditlens.storage import audit_events
from creditlens.workflow import QueryWorkflow

SCHEMA = build_schema("""
    type Viewer { subject: ID!, tenantId: ID!, grantRevision: Int! }
    type Catalog { revision: String!, documentCount: Int!, pageCount: Int!, chunkCount: Int! }
    type Audit { requestId: ID!, createdAt: String!, disposition: String, searchProvider: String }
    type Job { jobId: ID!, state: String!, attempts: Int!, errorCode: String }
    type Query {
        viewer: Viewer!
        catalog(borrowerId: ID!, effectiveAt: String!): Catalog!
        recentAudits(borrowerId: ID!, first: Int! = 10): [Audit!]!
        ingestionJob(id: ID!): Job!
    }
""")


def bounded_document(query: str, operation_name: str | None) -> DocumentNode:
    """Validate the real schema, then bound expanded fragments before any resolver runs."""
    document = parse(query, max_tokens=512)
    if validate(
        SCHEMA, document, rules=(*specified_rules, NoSchemaIntrospectionCustomRule), max_errors=5
    ):
        raise ValueError("Invalid GraphQL document")
    operation = get_operation_ast(document, operation_name)
    if operation is None or operation.operation != OperationType.QUERY:
        raise ValueError("A named read-only query is required")
    fragments = {
        node.name.value: node
        for node in document.definitions
        if isinstance(node, FragmentDefinitionNode)
    }
    pending: list[tuple[SelectionSetNode, int]] = [(operation.selection_set, 1)]
    fields, roots = 0, 0
    while pending:
        selections, depth = pending.pop()
        for node in selections.selections:
            fields += 1
            roots += int(depth == 1 and isinstance(node, FieldNode))
            if fields > 128 or roots > 8 or depth > 8:
                raise ValueError("GraphQL complexity limit exceeded")
            if isinstance(node, FragmentSpreadNode):
                pending.append((fragments[node.name.value].selection_set, depth))
            elif (
                isinstance(node, (FieldNode, InlineFragmentNode)) and node.selection_set is not None
            ):
                pending.append((node.selection_set, depth + int(isinstance(node, FieldNode))))
    return document


class AdminRoot:
    """Resolvers expose small allowlisted views, never protected query or packet JSON."""

    def __init__(
        self, workflow: QueryWorkflow, principal: Principal, jobs: JobStore | None
    ) -> None:
        """Share the existing workflow authority; a GraphQL request has no independent grants."""
        self.workflow, self.principal, self.jobs = workflow, principal, jobs
        self.revisions: list[int] = []

    def verify(self) -> None:
        """Discard the complete result if grants or any observed catalog revision changed."""
        if (
            self.principal.role != "admin"
            or self.workflow.store.resolve(self.principal.subject) != self.principal
        ):
            raise ServiceError("access_denied", "Administrator access is not current", 403)
        for revision in self.revisions:
            self.workflow.catalog.verify_revision(revision)

    def viewer(self, info: GraphQLResolveInfo) -> dict[str, Any]:
        """Report only the authenticated viewer rather than enumerating other principals."""
        return {
            "subject": self.principal.subject,
            "tenantId": self.principal.tenant_id,
            "grantRevision": self.principal.revision,
        }

    def catalog(
        self, info: GraphQLResolveInfo, borrowerId: str, effectiveAt: str
    ) -> dict[str, Any]:
        """Count only canonical chunks already filtered by current borrower, ACL and policy date."""
        chunks, revision = self.workflow.catalog.snapshot(
            self.principal, borrowerId, date.fromisoformat(effectiveAt)
        )
        self.revisions.append(revision)
        return {
            "revision": str(revision),
            "chunkCount": len(chunks),
            "documentCount": len({(c.document_id, c.document_version) for c in chunks}),
            "pageCount": len({(c.document_id, c.document_version, c.page) for c in chunks}),
        }

    def recentAudits(
        self, info: GraphQLResolveInfo, borrowerId: str, first: int = 10
    ) -> list[dict[str, Any]]:
        """Return bounded own-subject metadata; select no protected payload columns from SQL."""
        authorize_borrower(self.principal, borrowerId)
        if not 1 <= first <= 50:
            raise ValueError("Invalid audit page size")
        event = audit_events.c.event
        statement = (
            select(
                audit_events.c.request_id.label("requestId"),
                audit_events.c.created_at.label("createdAt"),
                event["disposition"].as_string().label("disposition"),
                event["search_provider_mode"].as_string().label("searchProvider"),
            )
            .where(
                audit_events.c.tenant_id == self.principal.tenant_id,
                audit_events.c.subject == self.principal.subject,
                event["borrower_id"].as_string() == borrowerId,
            )
            .order_by(audit_events.c.created_at.desc(), audit_events.c.request_id.desc())
            .limit(first)
        )
        with self.workflow.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def ingestionJob(self, info: GraphQLResolveInfo, id: str) -> dict[str, Any]:
        """Reuse scoped job status without exposing artifact content or lease tokens."""
        if self.jobs is None:
            raise ServiceError("ingestion_disabled", "Ingestion is not enabled", 503)
        job = self.jobs.status(str(UUID(id)), self.principal)
        return {
            "jobId": job.job_id,
            "state": job.state,
            "attempts": job.attempts,
            "errorCode": job.error_code,
        }


def execute_admin(
    query: str,
    variables: dict[str, Any],
    operation_name: str | None,
    workflow: QueryWorkflow,
    principal: Principal,
    jobs: JobStore | None,
) -> dict[str, Any]:
    """Return all authorized data or a curated error; never leak GraphQL partial data or inputs."""
    root = AdminRoot(workflow, principal, jobs)
    root.verify()
    try:
        document = bounded_document(query, operation_name)
    except (GraphQLError, ValueError, RecursionError) as error:
        raise ServiceError(
            "invalid_graphql", "GraphQL query is invalid or exceeds limits", 422
        ) from error
    result = execute_sync(
        SCHEMA, document, root_value=root, variable_values=variables, operation_name=operation_name
    )
    if result.errors:
        for result_error in result.errors:
            if isinstance(result_error.original_error, ServiceError):
                raise result_error.original_error
        raise ServiceError("graphql_failed", "GraphQL query could not be completed", 422)
    root.verify()
    return {"data": result.data}
