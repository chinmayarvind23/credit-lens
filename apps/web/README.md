# CreditLens web

CreditLens provides an underwriting evidence workbench using Bun 1.3.10, TypeScript 5.9.3, native HTML, and CSS. It renders backend-computed financial metrics and page-cited claims while keeping the lending decision with the underwriter.

## Run

From this directory:

```sh
bun install --frozen-lockfile
bun run typecheck
bun run test
bun run build
bun run dev
```

The Bun development UI at `http://localhost:3000` calls the backend at `http://localhost:8000`. For the integrated application, build first and start FastAPI using the repository setup instructions. FastAPI serves `apps/web/dist` and the API on the same origin. The browser never accepts an arbitrary API host or embeds backend credentials.

## Workflow

1. Load the server-authorized borrower list and environment mode.
2. Select a borrower and policy effective date.
3. Ask a question or edit one of the example questions.
4. Review the returned disposition, cited facts, backend-calculated metrics, gaps, conflicts, and next actions.
5. Open a citation to recheck source access and inspect literal extracted text, version, page, effective window, and provenance.
6. Expand the execution trace for actual provider mode, corpus version, request timing, cache status, stages, and cost when reported.

Synthetic demo mode is explicitly labeled and requires no access token. Production uses a managed Cognito access token supplied through the Connection panel. The token is held in memory, removed from the form after application, and never saved to browser storage. The managed sign-in redirect is not integrated.

## API contract

- `GET /api/v1/borrowers` returns `{borrowers, mode}`.
- `POST /api/v1/query` accepts `{borrower_id, question, effective_at}` and returns the typed underwriting packet.
- `GET /api/v1/evidence/{chunk_id}?borrower_id=...&effective_at=...` reauthorizes source inspection.

Wire contracts mirror `src/creditlens/domain.py` and are validated at runtime in `src/contracts.ts`. Decimal financial values remain strings for exact display. Claims without citations, citations inconsistent with retrieved evidence, malformed timing fields, and unknown dispositions are rejected as a failed response.

## State and security boundaries

The browser is untrusted. The backend resolves identity, tenant, borrower permissions, ACLs, and policy validity. Scope changes clear completed packets and cancel pending queries and source lookups. Completed results keep the original question and date. Older requests cannot overwrite newer state.

Dynamic text uses DOM text nodes. Source documents cannot introduce HTML, scripts, or external links. Requests use `no-store`, omit cookies, reject redirects, and have a 30-second deadline. Query retries require an explicit user action. Errors omit server response bodies and raw payloads. No source text, questions, or tokens are logged.

## Verification

`bun test` covers runtime contracts, transport behavior, cancellation, provenance checks, decimal preservation, and literal DOM rendering through a narrow document stand-in. The stand-in does not verify browser layout, focus, keyboard semantics, or assistive technology. Rendered end-to-end and mobile checks must be performed in a connected browser. Build output and dependencies are ignored by Git.
