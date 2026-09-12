# Internal gRPC contract

This optional interface reuses the existing authorization, workflow and audit
implementation. `creditlens.proto` defines borrower, question and effective-date
inputs. Its version-1 response contains UTF-8 Packet JSON bytes, preserving Decimal
strings and the existing Pydantic content contract. Generated clients use the
`creditlens.v1.Underwriting/Query` unary method.

```powershell
uv sync --locked --extra rpc
uv run --no-sync python -m infra.rpc.demo serve --port 50051 --duration 300
```

In another terminal:

```powershell
uv run --no-sync python -m infra.rpc.demo query --port 50051 --borrower borrower-001 --question "Prepare the DSCR underwriting packet"
```

The demo creates an isolated temporary SQLite database and binds only 127.0.0.1.
It uses public synthetic identity, needs no credentials and expires after the
specified duration. It is not an authenticated remote deployment. An embedding
host supplies `UnderwritingService` with its existing Authenticator, QueryWorkflow
and QueryLimiter. Signed-token tests exercise the production authenticator with
a fixture issuer and actual RSA signatures. Remote TLS/service deployment remains
an operator integration task.

Clients must set a deadline of at most 30 seconds. The listener limits request
bytes to 16 KiB, response bytes to approximately 1 MiB, workers to four and
concurrent RPCs to eight. Duplicate authorization metadata is rejected. Current
SQL grants and borrower scope are checked even for response-cache hits. Curated
status codes keep dependency exception text off the wire. Cancellation prevents
response delivery; synchronous work may finish and retain its audit after the
client deadline. Do not assume cancellation rolls back an acknowledged audit.

Actual socket tests cover all five lending dispositions, Decimal serialization,
fresh audits, warm-cache revocation, invalid credentials, wrong borrower, malformed
input, missing deadlines, duplicate metadata, size limits, quotas, private errors
and cancellation. Nine tests passed; service branch-inclusive coverage was 88%.

```powershell
uv run --no-sync pytest tests/test_grpc.py
uv run --no-sync python -m grpc_tools.protoc -I. --python_out=. --pyi_out=. --grpc_python_out=. infra/rpc/creditlens.proto
```

Generated protobuf files are compiler output, not handwritten application logic.
The committed lock pins grpcio/grpcio-tools 1.83.1 and protobuf 7.36.1.
See the official [Python tutorial](https://grpc.io/docs/languages/python/basics/)
and [deadline guide](https://grpc.io/docs/guides/deadlines/).
