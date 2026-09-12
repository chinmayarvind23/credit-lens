"""Run and call an isolated loopback-only synthetic gRPC demonstration."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import grpc

from creditlens.auth import Authenticator
from creditlens.domain import Packet
from creditlens.limits import create_query_limiter
from creditlens.runtime import open_workflow
from creditlens.settings import Settings
from creditlens.storage import GrantStore, open_database
from infra.rpc import creditlens_pb2, creditlens_pb2_grpc
from infra.rpc.service import UnderwritingService, serve_local


def serve_demo(port: int, duration: int) -> None:
    """Never reuse an existing grant database or expose this unauthenticated demo remotely."""
    with TemporaryDirectory(prefix="creditlens-rpc-demo-") as directory:
        settings = Settings(
            mode="demo", database_url=f"sqlite:///{Path(directory).as_posix()}/demo.db"
        )
        engine = open_database(settings.database_url)
        store = GrantStore(engine)
        store.seed_demo()
        limiter = create_query_limiter(settings)
        try:
            with open_workflow(settings, store) as workflow:
                if workflow is None:
                    raise RuntimeError("Demo workflow unavailable")
                service = UnderwritingService(Authenticator(settings, store), workflow, limiter)
                with serve_local(service, port) as target:
                    print(
                        f"Synthetic gRPC demo: {target}; expires after {duration} seconds",
                        flush=True,
                    )
                    Event().wait(duration)
        except KeyboardInterrupt:
            pass
        finally:
            limiter.close()
            engine.dispose()


def main() -> None:
    """Expose bounded demonstration commands without cloud credentials or caller-selected hosts."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("serve")
    server.add_argument("--port", type=int, default=50051)
    server.add_argument("--duration", type=int, default=300)
    client = sub.add_parser("query")
    client.add_argument("--port", type=int, default=50051)
    client.add_argument("--borrower", default="borrower-001")
    client.add_argument("--question", default="Prepare the DSCR underwriting packet")
    client.add_argument("--effective-at", default="2026-06-01")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if args.command == "serve":
        if not 1 <= args.duration <= 3600:
            parser.error("Duration must be between 1 and 3600 seconds")
        serve_demo(args.port, args.duration)
    else:
        with grpc.insecure_channel(f"127.0.0.1:{args.port}") as channel:
            response = creditlens_pb2_grpc.UnderwritingStub(channel).Query(
                creditlens_pb2.QueryRequest(
                    borrower_id=args.borrower,
                    question=args.question,
                    effective_at=args.effective_at,
                ),
                timeout=10,
            )
            if response.schema_version != 1:
                raise ValueError("Unsupported packet schema")
            print(Packet.model_validate_json(response.packet_json).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
