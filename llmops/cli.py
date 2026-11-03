"""Command line entry point: `llmops serve`, `llmops version`."""

import argparse

from llmops import __version__
from llmops.settings import get_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="llmops", description="Self-hosted LLM gateway.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--host", help="overrides LLMOPS_HOST")
    serve.add_argument("--port", type=int, help="overrides LLMOPS_PORT")
    serve.add_argument("--reload", action="store_true", help="restart on code changes (development)")

    sub.add_parser("version", help="print the version")

    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return

    import uvicorn  # only needed for serving, keeps `llmops version` fast

    settings = get_settings()
    uvicorn.run(
        "llmops.app:create_app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level=settings.log_level,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
