"""Command line: `haris serve`, `haris ingest <dir>`, `haris hash-key <key>`, `haris verify-audit`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from haris.config import get_settings
from haris.identity import Principal, hash_key

# Ingest from the CLI runs as a local operator with the admin role; the policy still checks it.
_OPERATOR = Principal(
    user_id="cli-operator",
    name="CLI operator",
    team="it-admin",
    department="it",
    clearance="restricted",
    roles=("admin",),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="haris", description="Haris AI Gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entrypoint
    serve.add_argument("--port", type=int, default=8080)
    ingest = sub.add_parser("ingest", help="ingest markdown files with front matter")
    ingest.add_argument("path", type=Path)
    hk = sub.add_parser("hash-key", help="print the SHA-256 hash to store for an API key")
    hk.add_argument("key")
    sub.add_parser("verify-audit", help="verify the audit hash chain")
    args = parser.parse_args(argv)

    if args.command == "hash-key":
        print(hash_key(args.key))
        return 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run("haris.app:create_app", factory=True, host=args.host, port=args.port)
        return 0

    from haris.app import build_gateway
    from haris.metrics import Metrics

    gateway, pool = build_gateway(get_settings(), Metrics())
    try:
        if args.command == "ingest":
            from haris.documents import load_markdown_documents

            docs = load_markdown_documents(args.path)
            n = gateway.ingest(_OPERATOR, docs)
            print(f"ingested {len(docs)} documents as {n} chunks")
        elif args.command == "verify-audit":
            s = gateway.audit.verify()
            print(f"audit chain {'intact' if s.ok else 'BROKEN at #' + str(s.first_bad_seq)} ({s.checked} records)")
            return 0 if s.ok else 1
    finally:
        if pool is not None:
            pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
