"""Command line: `rampart serve`, `rampart ingest <dir>`, `rampart new-key`, `rampart hash-key <key>`,
`rampart key-hashing`, `rampart verify-audit`."""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

import yaml

from rampart.config import get_settings
from rampart.identity import KeyHashing, Principal

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
    parser = argparse.ArgumentParser(prog="rampart", description="Rampart AI Gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entrypoint
    serve.add_argument("--port", type=int, default=8080)
    ingest = sub.add_parser("ingest", help="ingest markdown files with front matter")
    ingest.add_argument("path", type=Path)
    sub.add_parser("new-key", help="print a new random API key and the key_scrypt to store for it")
    hk = sub.add_parser("hash-key", help="print the key_scrypt to store for an API key you already have")
    hk.add_argument("key")
    sub.add_parser("key-hashing", help="print a key_hashing block with a new salt, for a new principals file")
    sub.add_parser("verify-audit", help="verify the audit hash chain")
    args = parser.parse_args(argv)

    if args.command in ("new-key", "hash-key"):
        # Hashed with the salt of the principals file the gateway reads, so the hash matches there.
        hashing = KeyHashing.from_file(get_settings().principals_file)
        key = secrets.token_urlsafe(32) if args.command == "new-key" else args.key
        if args.command == "new-key":
            print(f"api key:    {key}    (give it to its owner; it is stored nowhere)")
        print(f"key_scrypt: {hashing.digest(key)}")
        return 0

    if args.command == "key-hashing":
        print(yaml.safe_dump({"key_hashing": KeyHashing.generate().to_config()}, sort_keys=False), end="")
        return 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run("rampart.app:create_app", factory=True, host=args.host, port=args.port)
        return 0

    from rampart.app import build_gateway
    from rampart.metrics import Metrics

    gateway, pool = build_gateway(get_settings(), Metrics())
    try:
        if args.command == "ingest":
            from rampart.documents import load_markdown_documents

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
