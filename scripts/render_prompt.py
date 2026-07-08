#!/usr/bin/env python3
import argparse
import re
import sys
from pathlib import Path

PLACEHOLDER = re.compile(r"\[([A-Z0-9_]+)\]")


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_assignment(value: str) -> tuple[str, str]:
    if "=" not in value:
        fail(f"invalid --set value {value!r}; expected KEY=VALUE")
    key, replacement = value.split("=", 1)
    if not key or not re.fullmatch(r"[A-Z0-9_]+", key):
        fail(f"invalid placeholder key {key!r}")
    return key, replacement


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a markdown prompt template.")
    parser.add_argument("template", type=Path)
    parser.add_argument("--set", dest="assignments", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()

    if not args.template.is_file():
        fail(f"template not found: {args.template}")

    rendered = args.template.read_text(encoding="utf-8")
    template_keys = set(PLACEHOLDER.findall(rendered))

    replacements: dict[str, str] = {}
    for value in args.assignments:
        key, replacement = parse_assignment(value)
        if key in replacements:
            fail(f"duplicate --set key: {key}")
        replacements[key] = replacement

    unused = sorted(set(replacements) - template_keys)
    if unused:
        fail("unused replacement key(s): " + ", ".join(unused))

    for key, value in replacements.items():
        rendered = rendered.replace(f"[{key}]", value)

    remaining = sorted(set(PLACEHOLDER.findall(rendered)))
    if remaining:
        fail("unresolved placeholder(s): " + ", ".join(remaining))

    print(rendered)


if __name__ == "__main__":
    main()
