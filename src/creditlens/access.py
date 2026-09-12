"""Shared evidence scope rules independent of managed identity transports."""

from datetime import date

from creditlens.domain import Page, Principal
from creditlens.errors import ServiceError


def authorize_borrower(principal: Principal, borrower_id: str) -> None:
    """Deny before touching a search provider and use the same response for unknown IDs."""
    if borrower_id not in principal.borrower_ids:
        raise ServiceError("access_denied", "Access is not authorized", 403)


def authorized_page(page: Page, principal: Principal, borrower_id: str, effective_at: date) -> bool:
    """All search adapters use the same tenant, borrower, group and date predicate first."""
    return (
        borrower_id in principal.borrower_ids
        and page.tenant_id == principal.tenant_id
        and page.borrower_id in (None, borrower_id)
        and bool(set(page.acl_groups).intersection(principal.acl_groups))
        and page.valid_from <= effective_at
        and (page.valid_to is None or effective_at < page.valid_to)
        and page.extraction_confidence >= 0.9
    )
