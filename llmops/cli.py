"""Command line entry point: `llmops serve`, `llmops prompts ...`, `llmops keys new`, `llmops version`."""

import argparse
import difflib
import sys

import yaml

from llmops import __version__
from llmops.auth import hash_key, new_key
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

    keys = sub.add_parser("keys", help="manage team API keys")
    ksub = keys.add_subparsers(dest="action", required=True)
    new = ksub.add_parser("new", help="make a key for a team and print the gateway.yaml entry")
    new.add_argument("team")

    usage = sub.add_parser("usage", help="tokens and cost per team and model for a month")
    usage.add_argument("--db", help="usage database (default: LLMOPS_USAGE_DB or ./llmops-usage.db)")
    usage.add_argument("--month", help="YYYY-MM (default: this month, UTC)")
    usage.add_argument("--team", help="only this team")

    ev = sub.add_parser("eval", help="run eval suites against prompts and compare runs")
    esub = ev.add_subparsers(dest="action", required=True)
    run = esub.add_parser("run", help="run suites and print (or save) the scores")
    run.add_argument("suites", nargs="+", help="suite files, e.g. evals/*.yaml")
    run.add_argument("--prompts", help="prompt folder (default: LLMOPS_PROMPTS_DIR or ./prompts)")
    run.add_argument("--config", help="gateway config with the models (default: LLMOPS_CONFIG)")
    run.add_argument("--version", type=int, help="prompt version (default: the active one)")
    run.add_argument("--model", help="model to use instead of the one the prompt names")
    run.add_argument("--out", help="write the results as JSON, for `llmops eval compare`")
    run.add_argument("--min-score", type=float, default=0.0, help="exit code 1 below this score (0 to 1)")
    run.add_argument(
        "--skip-missing",
        action="store_true",
        help="skip suites whose prompt doesn't exist (for the base branch of a PR that adds a prompt)",
    )
    chk = esub.add_parser("check", help="validate suite files without calling any model")
    chk.add_argument("suites", nargs="+")
    chk.add_argument("--prompts", help="prompt folder (default: LLMOPS_PROMPTS_DIR or ./prompts)")
    cmp = esub.add_parser("compare", help="compare two saved runs; exit code 1 if a score dropped")
    cmp.add_argument("base")
    cmp.add_argument("new")
    cmp.add_argument("--max-drop", type=float, default=0.0, help="allowed score drop, e.g. 0.05")

    report = sub.add_parser("report", help="this week against last week: spend, cost per request, drift")
    report.add_argument("--db", help="usage database (default: LLMOPS_USAGE_DB or ./llmops-usage.db)")
    report.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    report.add_argument(
        "--fail-on-drift", action="store_true", help="exit code 2 when something drifted (for cron alerts)"
    )

    sub.add_parser("version", help="print the version")

    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return
    if args.command == "prompts":
        sys.exit(prompts_command(args))
    if args.command == "eval":
        from llmops.evals.cli import eval_command

        sys.exit(eval_command(args))
    if args.command == "report":
        sys.exit(report_command(args))
    if args.command == "usage":
        sys.exit(usage_command(args))
    if args.command == "keys":
        keys_new(args.team)
        return

    import uvicorn  # only needed for serving, keeps `llmops version` fast

    settings = get_settings()
    uvicorn.run(
        "llmops.app:serve_app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level=settings.log_level,
        reload=args.reload,
        # the gateway writes its own access log with request ids; see observability.py
        log_config=None,
        access_log=False,
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


def report_command(args: argparse.Namespace) -> int:
    from pathlib import Path

    from llmops.report import to_markdown, weekly_report
    from llmops.usage import SQLiteUsageStore

    db = args.db or get_settings().usage_db
    if not Path(db).is_file():
        print(f"error: no usage database at {db}", file=sys.stderr)
        return 1
    store = SQLiteUsageStore(db)
    try:
        report = weekly_report(store)
    finally:
        store.close()
    print(report.model_dump_json(indent=2) if args.json else to_markdown(report), end="")
    return 2 if args.fail_on_drift and report.drift else 0


def usage_command(args: argparse.Namespace) -> int:
    from pathlib import Path

    from llmops.budgets import month_range
    from llmops.usage import SQLiteUsageStore

    db = args.db or get_settings().usage_db
    if not Path(db).is_file():
        print(f"error: no usage database at {db}", file=sys.stderr)
        return 1
    try:
        start, end = month_range(args.month)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    store = SQLiteUsageStore(db)
    try:
        rows = store.summary(start, end, team=args.team)
    finally:
        store.close()
    print(f"usage for {start:%Y-%m} (UTC)\n")
    header = (
        f"{'team':<16} {'model':<24} {'requests':>9} {'tokens in':>11} {'tokens out':>11} {'cost USD':>11}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r.team:<16} {r.model:<24} {r.requests:>9} {r.prompt_tokens:>11} "
            f"{r.completion_tokens:>11} {r.cost_usd:>11.4f}"
        )
    total = sum(r.cost_usd for r in rows)
    print("-" * len(header))
    print(f"{'total':<16} {'':<24} {sum(r.requests for r in rows):>9} {'':>11} {'':>11} {total:>11.4f}")
    return 0


def keys_new(team: str) -> None:
    key = new_key()
    print(f"key for {team}: {key}")
    print("give it to the team now; only its hash is kept. add this under `teams:` in gateway.yaml:\n")
    print(f"  - name: {team}\n    key_sha256: {hash_key(key)}")


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
