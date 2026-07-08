#!/usr/bin/env python3
import argparse
import json
import re
import sys
from pathlib import Path

TICKET_HEADING = re.compile(r"^### Ticket ([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+) - (.+?)\s*$", re.MULTILINE)
MAX_IMPLEMENTATION_SURFACES = 2
MAX_EXPECTED_FILE_MODULE_ITEMS = 8
PROOF_COMMAND_PREFIXES = (
    "bun ",
    "npm ",
    "pnpm ",
    "yarn ",
    "node ",
    "npx ",
    "vitest ",
    "jest ",
    "pytest ",
    "python ",
    "python3 ",
    "go ",
    "cargo ",
    "ruff ",
    "tsc ",
    "git ",
)
MAJOR_SURFACES = {
    "runtime control flow",
    "provider integration",
    "schema/data model",
    "ui/api surface",
    "legacy deletion",
    "queue/replay/idempotency",
    "external contract changes",
}
RECOVERY_SCOPE_KEYWORDS = re.compile(
    r"\b(retry|retries|retryable|cleanup|clean up|durable failure|failure accounting|idempotenc|post-store|mutation sequencing)\b",
    re.IGNORECASE,
)
RECOVERY_BEHAVIOR_PATTERNS = {
    "classification": re.compile(r"\b(classif|retryable|unknown error|error type)\b", re.IGNORECASE),
    "mutation sequencing": re.compile(r"\b(sequence|ordering|before cleanup|after cleanup|mutation)\b", re.IGNORECASE),
    "cleanup": re.compile(r"\b(cleanup|clean up|delete stored|delete blob|storage cleanup)\b", re.IGNORECASE),
    "durable failure accounting": re.compile(r"\b(durable failure|failure accounting|record failed|copy_failed|retry attempt|attempt count)\b", re.IGNORECASE),
    "idempotency": re.compile(r"\b(idempotenc|replay|duplicate|already processed)\b", re.IGNORECASE),
}
REQUIRED_PLAN_SECTIONS = [
    "Description of Overarching Goal",
    "Ample Technical Context",
    "Explanation of Plan",
    "Tickets",
    "Deliverables",
    "Updates to",
]
REQUIRED_TICKET_SUBSECTIONS = [
    "Detailed description of the goal of the ticket",
    "Ample technical context from the codebase",
    "Quotations and accompanying citations",
    "Primary invariant",
    "Touched surfaces",
    "Non-goals",
    "Follow-up boundary",
    "Detailed, targeted, specific code snippets and specifications",
    "Deliverables",
    "Test case descriptions",
]
TICKET_TABLE_HEADER = [
    "Ticket",
    "Target repo",
    "Title",
    "Dependencies",
    "Independent group",
    "Expected files/modules",
    "Required verification",
]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def has_section(text: str, needle: str) -> bool:
    return re.search(rf"^##+\s+.*{re.escape(needle)}", text, re.MULTILINE | re.IGNORECASE) is not None


def parse_table_line(line: str) -> list[str]:
    if not line.startswith("|") or not line.endswith("|"):
        return []
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def parse_ticket_index(text: str) -> dict[str, dict[str, str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        header = parse_table_line(line)
        if header != TICKET_TABLE_HEADER:
            continue

        if index + 1 >= len(lines):
            fail("ticket index table is missing separator row")
        separator = parse_table_line(lines[index + 1])
        if not is_separator(separator) or len(separator) != len(header):
            fail("ticket index table has invalid separator row")

        rows: dict[str, dict[str, str]] = {}
        for row_line in lines[index + 2:]:
            cells = parse_table_line(row_line)
            if not cells:
                break
            if len(cells) != len(header):
                fail(f"ticket index row has {len(cells)} cell(s), expected {len(header)}: {row_line}")
            row = dict(zip(header, cells, strict=True))
            ticket_id = row["Ticket"]
            if not TICKET_HEADING.fullmatch(f"### Ticket {ticket_id} - placeholder"):
                fail(f"ticket index row has invalid ticket id: {ticket_id}")
            if ticket_id in rows:
                fail(f"duplicate ticket id in ticket index: {ticket_id}")
            rows[ticket_id] = row

        if not rows:
            fail("ticket index table has no ticket rows")
        return rows

    fail("missing required ticket index table with Target repo column")


def parse_dependencies(value: str) -> list[str]:
    if value.lower() == "none":
        return []
    dependencies = [dependency.strip() for dependency in value.split(",")]
    if any(not dependency for dependency in dependencies):
        fail(f"invalid dependency list: {value}")
    return dependencies


def require_non_empty(ticket_id: str, row: dict[str, str], key: str) -> None:
    value = row[key].strip()
    if not value or value.lower() == "none":
        fail(f"{ticket_id} has empty {key}")


def subsection_text(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"^#+\s+{re.escape(heading)}\s*$|^\d+\.\s+{re.escape(heading)}\.?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(body)
    if not match:
        return ""
    next_heading = re.search(r"^(?:#+\s+|\d+\.\s+).+", body[match.end():], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(body)
    return body[match.end():end].strip()


def split_list_items(value: str) -> list[str]:
    items = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^(?:[-*]|\d+\.)\s+", "", stripped).strip()
        if stripped:
            items.append(stripped)
    if items:
        return items
    return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]


def validate_primary_invariant(ticket_id: str, body: str) -> None:
    invariant = subsection_text(body, "Primary invariant")
    if not invariant:
        fail(f"{ticket_id} missing Primary invariant content")
    first_line = invariant.splitlines()[0].strip()
    if re.search(r"\s+(and|also|plus)\s+", first_line, re.IGNORECASE):
        fail(f"{ticket_id} primary invariant appears to combine multiple outcomes; split the ticket: {first_line}")


def validate_touched_surfaces(ticket_id: str, body: str) -> None:
    text = subsection_text(body, "Touched surfaces")
    if not text:
        fail(f"{ticket_id} missing Touched surfaces content")
    surfaces = [item.lower().strip(" .") for item in split_list_items(text)]
    implementation_surfaces = [surface for surface in surfaces if surface not in {"tests", "documentation", "docs"}]
    if len(implementation_surfaces) > MAX_IMPLEMENTATION_SURFACES:
        fail(
            f"{ticket_id} touches {len(implementation_surfaces)} implementation surfaces; "
            f"maximum is {MAX_IMPLEMENTATION_SURFACES}: {', '.join(implementation_surfaces)}"
        )
    unknown = [surface for surface in implementation_surfaces if surface not in MAJOR_SURFACES]
    if unknown:
        allowed = ", ".join(sorted(MAJOR_SURFACES | {"tests", "documentation"}))
        fail(f"{ticket_id} has unknown touched surface(s): {', '.join(unknown)}; allowed: {allowed}")
    if {"runtime control flow", "legacy deletion", "schema/data model"}.issubset(set(implementation_surfaces)):
        fail(f"{ticket_id} combines runtime behavior, schema changes, and legacy deletion; split it")
    if {"ui/api surface", "legacy deletion", "runtime control flow"}.issubset(set(implementation_surfaces)):
        fail(f"{ticket_id} combines runtime behavior, UI/API cleanup, and legacy deletion; split it")


def validate_non_goals_and_boundary(ticket_id: str, body: str) -> None:
    non_goals = subsection_text(body, "Non-goals")
    if not non_goals or non_goals.lower() == "none":
        fail(f"{ticket_id} must include explicit Non-goals")
    boundary = subsection_text(body, "Follow-up boundary")
    if not boundary or boundary.lower() == "none":
        fail(f"{ticket_id} must include an explicit Follow-up boundary")
    if "follow" not in boundary.lower() and "out of scope" not in boundary.lower():
        fail(f"{ticket_id} Follow-up boundary must say what becomes follow-up or out-of-scope work")


def validate_expected_files_modules(ticket_id: str, value: str) -> None:
    items = split_list_items(value)
    if len(items) > MAX_EXPECTED_FILE_MODULE_ITEMS:
        fail(
            f"{ticket_id} lists {len(items)} expected files/modules; "
            f"maximum is {MAX_EXPECTED_FILE_MODULE_ITEMS}"
        )
    vague = [item for item in items if item.lower() in {"multiple files", "various files", "tbd", "unknown"}]
    if vague:
        fail(f"{ticket_id} has vague Expected files/modules item(s): {', '.join(vague)}")


def validate_required_verification(ticket_id: str, value: str) -> None:
    if re.search(r"\b(relevant|appropriate|as needed|tests pass|tbd)\b", value, re.IGNORECASE):
        fail(f"{ticket_id} Required verification is too vague: {value}")
    commands = [match.group(1).strip() for match in re.finditer(r"`([^`]+)`", value)]
    runnable = [command for command in commands if command.startswith(PROOF_COMMAND_PREFIXES)]
    if not runnable and not re.search(r"\b(no helper-runnable|manual-only|not helper-runnable)\b", value, re.IGNORECASE):
        fail(
            f"{ticket_id} Required verification must include at least one concrete backticked helper-runnable command "
            "or explicitly state that verification is not helper-runnable"
        )


def validate_recovery_ticket_scope(ticket_id: str, title: str, body: str) -> None:
    scoped_text = "\n".join(
        [
            title,
            subsection_text(body, "Primary invariant"),
            subsection_text(body, "Detailed, targeted, specific code snippets and specifications"),
            subsection_text(body, "Deliverables"),
        ]
    )
    if not RECOVERY_SCOPE_KEYWORDS.search(scoped_text):
        return
    matched = [
        name
        for name, pattern in RECOVERY_BEHAVIOR_PATTERNS.items()
        if pattern.search(scoped_text)
    ]
    if len(matched) > 1:
        fail(
            f"{ticket_id} recovery/control-flow ticket combines multiple behavior changes "
            f"({', '.join(matched)}); split classification, mutation sequencing, cleanup, "
            "durable failure accounting, and idempotency into separate tickets unless the plan proves they are inseparable"
        )


def validate_target_repo(ticket_id: str, target_repo: str, allowed_target_repos: set[str]) -> None:
    if not target_repo or target_repo.lower() == "none":
        fail(f"{ticket_id} missing Target repo")
    if any(token in target_repo for token in [",", ";", "+", "&"]):
        fail(f"{ticket_id} Target repo must name exactly one repo: {target_repo}")
    if " and " in target_repo.lower():
        fail(f"{ticket_id} Target repo must name exactly one repo: {target_repo}")
    if allowed_target_repos and target_repo not in allowed_target_repos:
        allowed = ", ".join(sorted(allowed_target_repos))
        fail(f"{ticket_id} Target repo {target_repo!r} is not allowed; expected one of: {allowed}")


def validate_dependencies(tickets: list[dict[str, object]]) -> None:
    ids = {str(ticket["id"]) for ticket in tickets}
    graph = {str(ticket["id"]): list(ticket["dependencies"]) for ticket in tickets}

    for ticket_id, dependencies in graph.items():
        for dependency in dependencies:
            if dependency not in ids:
                fail(f"{ticket_id} depends on unknown ticket {dependency}")
            if dependency == ticket_id:
                fail(f"{ticket_id} cannot depend on itself")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ticket_id: str, path: list[str]) -> None:
        if ticket_id in visited:
            return
        if ticket_id in visiting:
            fail("ticket dependency cycle detected: " + " -> ".join(path + [ticket_id]))
        visiting.add(ticket_id)
        for dependency in graph[ticket_id]:
            visit(dependency, path + [ticket_id])
        visiting.remove(ticket_id)
        visited.add(ticket_id)

    for ticket_id in graph:
        visit(ticket_id, [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a ship-loop plan and print ticket JSON.")
    parser.add_argument("plan", type=Path)
    parser.add_argument("--allowed-target-repo", action="append", required=True, metavar="REPO")
    args = parser.parse_args()

    plan_path = args.plan
    if not plan_path.is_file():
        fail(f"plan file not found: {plan_path}")

    text = plan_path.read_text(encoding="utf-8")

    missing_sections = [section for section in REQUIRED_PLAN_SECTIONS if not has_section(text, section)]
    if missing_sections:
        fail("missing required plan section(s): " + ", ".join(missing_sections))

    ticket_rows = parse_ticket_index(text)
    allowed_target_repos = set(args.allowed_target_repo)

    matches = list(TICKET_HEADING.finditer(text))
    if not matches:
        fail("no canonical ticket headings found; expected '### Ticket ABC-01 - Title'")

    tickets = []
    seen = set()
    for index, match in enumerate(matches):
        ticket_id = match.group(1)
        title = match.group(2).strip()
        if ticket_id in seen:
            fail(f"duplicate ticket id: {ticket_id}")
        seen.add(ticket_id)
        if ticket_id not in ticket_rows:
            fail(f"{ticket_id} missing from ticket index table")

        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end]
        missing = [
            subsection
            for subsection in REQUIRED_TICKET_SUBSECTIONS
            if subsection.lower() not in body.lower()
        ]
        if missing:
            fail(f"{ticket_id} missing required subsection(s): " + ", ".join(missing))
        validate_primary_invariant(ticket_id, body)
        validate_touched_surfaces(ticket_id, body)
        validate_non_goals_and_boundary(ticket_id, body)
        validate_recovery_ticket_scope(ticket_id, title, body)

        target_repo = ticket_rows[ticket_id]["Target repo"]
        table_title = ticket_rows[ticket_id]["Title"]
        if table_title != title:
            fail(f"{ticket_id} title mismatch: heading has {title!r}, table has {table_title!r}")
        validate_target_repo(ticket_id, target_repo, allowed_target_repos)
        dependencies = parse_dependencies(ticket_rows[ticket_id]["Dependencies"])
        require_non_empty(ticket_id, ticket_rows[ticket_id], "Expected files/modules")
        require_non_empty(ticket_id, ticket_rows[ticket_id], "Required verification")
        validate_expected_files_modules(ticket_id, ticket_rows[ticket_id]["Expected files/modules"])
        validate_required_verification(ticket_id, ticket_rows[ticket_id]["Required verification"])
        ticket = {
            "id": ticket_id,
            "title": title,
            "target_repo": target_repo,
            "dependencies": dependencies,
            "independent_group": ticket_rows[ticket_id]["Independent group"],
            "expected_files_modules": ticket_rows[ticket_id]["Expected files/modules"],
            "required_verification": ticket_rows[ticket_id]["Required verification"],
        }
        tickets.append(ticket)

    indexed_only = sorted(set(ticket_rows) - seen)
    if indexed_only:
        fail("ticket index row(s) without canonical heading: " + ", ".join(indexed_only))

    validate_dependencies(tickets)
    print(json.dumps({"plan": str(plan_path), "tickets": tickets}, indent=2))


if __name__ == "__main__":
    main()
