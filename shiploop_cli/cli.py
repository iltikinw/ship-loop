from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from shiploop_cli import __version__
from shiploop_cli.discovery import (
    CONFIG_PATH,
    DiscoveryError,
    add_configured_root,
    load_configured_roots,
    record_from_state_file,
    remove_configured_root,
    resolve_slug,
)


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(argv if argv is not None else sys.argv[1:])
    if args_list and args_list[0] == "roots":
        return _roots_main(args_list[1:])

    parser = _build_parser()
    args = parser.parse_args(args_list)
    if args.version:
        print(__version__)
        return 0
    try:
        if args.state_file and args.slug:
            raise DiscoveryError("use either [slug] or --state-file, not both")
        if args.state_file:
            record = record_from_state_file(args.state_file)
        else:
            if not args.slug:
                raise DiscoveryError("missing ship-loop slug")
            record = resolve_slug(args.slug, explicit_roots=args.search_root)
        from shiploop_cli.app import ShiploopApp

        ShiploopApp(record).run()
        return 0
    except DiscoveryError as exc:
        print(f"shiploop: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shiploop", description="Terminal UI for ship-loop runs.")
    parser.add_argument("slug", nargs="?", help="Ship-loop plan slug to open.")
    parser.add_argument("--state-file", type=Path, help="Open a specific ship-loop state.json.")
    parser.add_argument("--search-root", action="append", default=[], type=Path, help="Root containing .ship-loop.")
    parser.add_argument("--version", action="store_true", help="Show shiploop version and exit.")
    return parser


def _roots_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="shiploop roots", description="Manage shiploop search roots.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List configured search roots.")
    add_parser = subparsers.add_parser("add", help="Add a configured search root.")
    add_parser.add_argument("path", type=Path)
    remove_parser = subparsers.add_parser("remove", help="Remove a configured search root.")
    remove_parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            for root in load_configured_roots():
                print(root)
            return 0
        if args.command == "add":
            roots = add_configured_root(args.path)
        elif args.command == "remove":
            roots = remove_configured_root(args.path)
        else:
            raise DiscoveryError(f"unknown roots command: {args.command}")
        print(f"config: {CONFIG_PATH}")
        for root in roots:
            print(root)
        return 0
    except DiscoveryError as exc:
        print(f"shiploop roots: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
