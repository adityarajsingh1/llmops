"""Command line entry point: `llmops serve`, `llmops prompts ...`, `llmops version`."""

import argparse
import difflib
import sys

import yaml

from llmops import __version__
from llmops.prompts import PromptError, PromptRegistry, PromptVersion
from llmops.settings import get_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="llmops", description="Self-hosted LLM gateway.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--host", help="overrides LLMOPS_HOST")
    serve.add_argument("--port", type=int, help="overrides LLMOPS_PORT")
    serve.add_argument("--reload", action="store_true", help="restart on code changes (development)")

    prompts = sub.add_parser("prompts", help="inspect the prompt registry")
    prompts.add_argument("--dir", help="prompt folder (default: LLMOPS_PROMPTS_DIR or ./prompts)")
    psub = prompts.add_subparsers(dest="action", required=True)
    psub.add_parser("list", help="every prompt with its versions")
    psub.add_parser("check", help="validate all prompt files (exit code 1 on errors, handy in CI)")
    show = psub.add_parser("show", help="print one version of a prompt")
    show.add_argument("name")
    show.add_argument("--version", type=int, help="default: the active version")
    diff = psub.add_parser("diff", help="what changed between two versions")
    diff.add_argument("name")
    diff.add_argument("old", type=int)
    diff.add_argument("new", type=int)

    sub.add_parser("version", help="print the version")

    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return
    if args.command == "prompts":
        sys.exit(prompts_command(args))

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


class _BlockDumper(yaml.SafeDumper):
    """Writes multi-line strings as | blocks, so diffs show real lines instead of escaped ones."""


def _str_presenter(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_BlockDumper.add_representer(str, _str_presenter)


def _as_text(v: PromptVersion) -> str:
    data = v.model_dump(exclude_none=True, exclude={"version", "note"})
    return yaml.dump(data, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True, width=100)


def prompts_command(args: argparse.Namespace) -> int:
    folder = args.dir or get_settings().prompts_dir
    try:
        registry = PromptRegistry.from_dir(folder)
        if args.action == "check":
            print(f"ok: {len(registry.names())} prompts in {folder}")
        elif args.action == "list":
            for name in registry.names():
                p = registry[name]
                versions = ", ".join(
                    f"v{v.version}" + ("*" if v is p.active_version else "") for v in p.versions
                )
                print(f"{name:<24} {versions:<16} {p.description}")
            print("\n* = active version")
        elif args.action == "show":
            v = registry[args.name].get(args.version)
            print(f"# {args.name} v{v.version}" + (f" ({v.note})" if v.note else ""))
            print(f"# variables: {', '.join(sorted(v.variables)) or 'none'}")
            print(_as_text(v), end="")
        elif args.action == "diff":
            p = registry[args.name]
            old, new = _as_text(p.get(args.old)), _as_text(p.get(args.new))
            lines = difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"{args.name} v{args.old}",
                tofile=f"{args.name} v{args.new}",
            )
            print("".join(lines) or "no differences", end="")
    except PromptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    main()
