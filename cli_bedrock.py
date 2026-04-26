"""CLI subcommand group for bedrock credential management.

See issue #405 acceptance criterion 6. Implemented as a separate module
so the argparse setup in ``agent.py`` can stay additive (cycle 79
forbids whole-file rewrites of ``agent.py``).

Wire-up: ``agent.main`` calls ``maybe_dispatch(argv)``; if the first
positional is ``bedrock`` we handle it here and ``sys.exit`` with the
subcommand's return code, otherwise we return ``None`` and the regular
parser runs.

Subcommands (all log ``bedrock.cli.<verb>`` at INFO per criterion 10):

- ``add --name <n> --url <u> --key <k>`` — append entry, run health
  check, set ``up``/``down`` based on result.
- ``list [--json]`` — table or JSON dump.
- ``rm <name> [--yes]`` — remove by name; prompts unless ``--yes``.
- ``retest [<name> | --all]`` — re-run the health probe.
- ``prune [--stale-days N] [--yes]`` — drop entries that are
  ``status==down`` and older than ``N`` days (default 30).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Sequence

import bedrock_store as bs


def _log() -> logging.Logger:
    return logging.getLogger("agent")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent bedrock",
        description="Manage the bedrock credential store.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_add = sub.add_parser("add", help="Add a credential entry.")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--url", required=True)
    p_add.add_argument("--key", required=True)

    p_list = sub.add_parser("list", help="List credential entries.")
    p_list.add_argument("--json", dest="as_json", action="store_true",
                        help="Dump the full file as JSON.")

    p_rm = sub.add_parser("rm", help="Remove an entry by name.")
    p_rm.add_argument("name")
    p_rm.add_argument("--yes", action="store_true",
                      help="Skip the confirmation prompt.")

    p_re = sub.add_parser("retest", help="Re-run the health probe.")
    p_re.add_argument("name", nargs="?", default=None)
    p_re.add_argument("--all", dest="all_entries", action="store_true",
                      help="Re-test every entry, including up ones.")

    p_pr = sub.add_parser("prune", help="Remove stale down entries.")
    p_pr.add_argument("--stale-days", type=int, default=30,
                      help="Down entries older than N days are removed (default 30).")
    p_pr.add_argument("--yes", action="store_true",
                      help="Skip the confirmation prompt.")

    return parser


def _truncate(s: str | None, n: int = 60) -> str:
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_table(entries: list[dict]) -> str:
    if not entries:
        return "(no entries)"
    headers = ["NAME", "STATUS", "SPEND", "LAST_CHECKED", "LAST_ERROR"]
    rows = [
        [
            str(e.get("name", "")),
            str(e.get("status", "")),
            f"{float(e.get('daily_spend_usd') or 0.0):.2f}",
            str(e.get("last_checked") or ""),
            _truncate(e.get("last_error"), 40),
        ]
        for e in entries
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    lines = [fmt.format(*headers)]
    for r in rows:
        lines.append(fmt.format(*r))
    return "\n".join(lines)


def cmd_add(args: argparse.Namespace) -> int:
    log = _log()
    log.info("bedrock.cli.add name=%s url=%s", args.name, args.url)
    with bs.with_locked_store() as locked:
        if locked is None:
            print("error: could not lock the store (another process is mutating it)",
                  file=sys.stderr)
            return 1
        data, path = locked
        try:
            entry = bs.add_entry(
                data, name=args.name, url=args.url, key=args.key,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        # Probe before persisting so the row goes in with its real status.
        ok, err = bs.health_check(args.url, args.key)
        entry["status"] = bs.STATUS_UP if ok else bs.STATUS_DOWN
        entry["last_checked"] = bs._now_iso()
        entry["last_error"] = None if ok else err
        bs.write_store(data, path)
    status = "up" if ok else "down"
    print(f"added {args.name} ({status})")
    if not ok and err:
        print(f"  last_error: {err}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    log = _log()
    log.info("bedrock.cli.list json=%s", bool(args.as_json))
    data = bs.load_store()
    if args.as_json:
        print(json.dumps(data, indent=2))
        return 0
    print(_format_table(data.get("entries", [])))
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    log = _log()
    log.info("bedrock.cli.rm name=%s yes=%s", args.name, args.yes)
    if not args.yes:
        try:
            ans = input(f"remove entry {args.name!r}? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted")
            return 1
    with bs.with_locked_store() as locked:
        if locked is None:
            print("error: could not lock the store", file=sys.stderr)
            return 1
        data, path = locked
        if not bs.remove_entry(data, args.name):
            print(f"error: entry {args.name!r} not found", file=sys.stderr)
            return 1
        bs.write_store(data, path)
    print(f"removed {args.name}")
    return 0


def cmd_retest(args: argparse.Namespace) -> int:
    log = _log()
    log.info("bedrock.cli.retest name=%s all=%s", args.name, args.all_entries)
    if not args.all_entries and not args.name:
        print("error: pass <name> or --all", file=sys.stderr)
        return 1
    with bs.with_locked_store() as locked:
        if locked is None:
            print("error: could not lock the store", file=sys.stderr)
            return 1
        data, path = locked
        if args.all_entries:
            targets = list(data.get("entries", []))
        else:
            ent = bs.find_entry(data, args.name)
            if ent is None:
                print(f"error: entry {args.name!r} not found", file=sys.stderr)
                return 1
            targets = [ent]
        if not targets:
            print("(no entries)")
            return 0
        for ent in targets:
            ok, err = bs.health_check(
                str(ent.get("url", "")), str(ent.get("key", "")),
            )
            ent["status"] = bs.STATUS_UP if ok else bs.STATUS_DOWN
            ent["last_checked"] = bs._now_iso()
            ent["last_error"] = None if ok else err
            print(f"{ent.get('name')}: {ent['status']}"
                  + (f"  ({err})" if not ok and err else ""))
        bs.write_store(data, path)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    log = _log()
    log.info("bedrock.cli.prune stale_days=%d yes=%s", args.stale_days, args.yes)
    data = bs.load_store()
    victims = [
        e for e in data.get("entries", [])
        if bs.is_stale(e, args.stale_days)
    ]
    if not victims:
        print("(nothing to prune)")
        return 0
    if not args.yes:
        names = ", ".join(str(v.get("name")) for v in victims)
        try:
            ans = input(
                f"prune {len(victims)} entr{'y' if len(victims) == 1 else 'ies'} "
                f"({names})? [y/N] "
            ).strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted")
            return 1
    with bs.with_locked_store() as locked:
        if locked is None:
            print("error: could not lock the store", file=sys.stderr)
            return 1
        data, path = locked
        before = len(data.get("entries", []))
        data["entries"] = [
            e for e in data.get("entries", [])
            if not bs.is_stale(e, args.stale_days)
        ]
        removed = before - len(data["entries"])
        bs.write_store(data, path)
    print(f"pruned {removed} entries")
    return 0


_DISPATCH = {
    "add": cmd_add,
    "list": cmd_list,
    "rm": cmd_rm,
    "retest": cmd_retest,
    "prune": cmd_prune,
}


def run(argv: Sequence[str]) -> int:
    """Parse ``argv`` (without the leading ``bedrock`` token) and dispatch."""
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    handler = _DISPATCH.get(args.verb)
    if handler is None:  # pragma: no cover - argparse already enforces this
        parser.error(f"unknown verb {args.verb!r}")
    return handler(args)


def maybe_dispatch(argv: Sequence[str] | None = None) -> int | None:
    """If ``argv[1] == 'bedrock'``, run the subcommand and return its
    exit code. Otherwise return ``None`` so ``agent.main`` keeps going.
    """
    av = list(sys.argv[1:] if argv is None else argv)
    if not av or av[0] != "bedrock":
        return None
    return run(av[1:])
