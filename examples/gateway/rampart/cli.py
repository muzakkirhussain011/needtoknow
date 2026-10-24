"""Command line: `rampart serve`, `rampart ingest <dir>`, `rampart new-key`, `rampart hash-key <key>`,
`rampart key-hashing`, `rampart verify-audit`."""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

import yaml

from rampart.config import get_settings
from rampart.identity import KeyHashing, Principal, key_hashing

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
    serve = sub.add_parser("serve", help="run the HTTP API, and Prometheus metrics on a port of their own")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entrypoint
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--metrics-host", help="where metrics are served (default RAMPART_METRICS_HOST, 127.0.0.1)")
    serve.add_argument("--metrics-port", type=int, help="metrics port (default RAMPART_METRICS_PORT, 9464; 0: none)")
    ingest = sub.add_parser("ingest", help="ingest markdown files with front matter")
    ingest.add_argument("path", type=Path)
    sub.add_parser("new-key", help="print a new random API key and the key_hmac to store for it")
    hk = sub.add_parser("hash-key", help="print the key_hmac to store for an API key you already have")
    hk.add_argument("key")
    sub.add_parser("key-hashing", help="print a new key secret and the key_hashing block for a new principals file")
    sub.add_parser("verify-audit", help="verify the audit hash chain")
    args = parser.parse_args(argv)
    settings = get_settings()
    secret = settings.key_secret.get_secret_value() if settings.key_secret is not None else ""

    if args.command in ("new-key", "hash-key"):
        # Hashed under the secret the gateway runs with, so the digest matches there.
        try:
            hashing = key_hashing(secret, settings.demo)
        except ValueError as exc:
            print(f"rampart: {exc}", file=sys.stderr)
            return 2
        key = secrets.token_urlsafe(32) if args.command == "new-key" else args.key
        if args.command == "new-key":
            print(f"api key:  {key}    (give it to its owner; it is stored nowhere)")
        print(f"key_hmac: {hashing.digest(key)}")
        return 0

    if args.command == "key-hashing":
        hashing = KeyHashing.generate()
        print(f"RAMPART_KEY_SECRET={hashing.secret.hex()}    (the gateway's environment only; never in git)")
        print()
        print(yaml.safe_dump({"key_hashing": hashing.to_config()}, sort_keys=False), end="")
        return 0

    if args.command == "serve":
        import prometheus_client
        import uvicorn

        from rampart.app import create_app, load_directory

        try:  # the credentials are checked before anything listens
            directory = load_directory(settings)
        except ValueError as exc:
            print(f"rampart: {exc}", file=sys.stderr)
            return 2
        app = create_app(settings, directory=directory)
        metrics_port = settings.metrics_port if args.metrics_port is None else args.metrics_port
        if metrics_port:
            # Apart from the API, so they can stay on an internal network; nothing there is secret,
            # but it tells anyone who can read it how the gateway is used.
            metrics_host = args.metrics_host or settings.metrics_host
            prometheus_client.start_http_server(metrics_port, addr=metrics_host, registry=app.state.metrics.registry)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    from rampart.app import build_gateway
    from rampart.metrics import Metrics

    try:  # the audit log's question fingerprints are keyed with the gateway's secret
        gateway, pool = build_gateway(settings, Metrics())
    except ValueError as exc:
        print(f"rampart: {exc}", file=sys.stderr)
        return 2
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
