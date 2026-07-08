# Workspace Mode

## Purpose

Run a repository-owned shipping loop using prompt templates committed inside the target repo. The primary UX is a multi-turn planning handoff: the user can scope requirements with an agent over many turns, then invoke this skill to turn the full current conversation into a committed plan and autonomous ticket loop.

The skill also supports an explicit workspace mode for a parent directory that orchestrates multiple child repositories while one repository owns all plans and internal docs. Workspace mode is active only when the user writes `Mode: workspace`.

The skill does not impose one product domain. In regular mode, the target repo owns its details through these files. In workspace mode, the plan owner repo owns the plan and ticket prompt details through these files, while each target repo contributes its own repository agent instructions:

1. `agent-prompts/plan-structure.md`
2. `agent-prompts/ticket-implementation.md`
3. repository agent instructions, such as `AGENTS.md` when present

Do not invent fallbacks. If a required tool, prompt template, authority doc named by the repo, worktree, or verification step is unavailable, stop and report the exact blocker.

## Repo Contract

In regular single-repo mode, the target repo must contain these files at repo root:

- `agent-prompts/plan-structure.md`
- `agent-prompts/ticket-implementation.md`

In workspace mode, the plan owner repo's configured plan prompt root must contain these files. Target repos do not need their own prompt templates unless the workspace prompt explicitly requires them.

The plan prompt owns the plan output path and required plan structure. If the plan prompt does not make the output path or fallback path unambiguous, stop before creating the plan.

## Workspace Mode Contract

Workspace mode is active only when the user explicitly provides `Mode: workspace`. Do not infer workspace mode from parent directories, sibling repositories, repository names, or the existence of a workspace prompt.

When workspace mode is active:

1. Treat the invocation directory as the workspace root.
2. Load exactly `agent-prompts/workspace-mode.md` from the workspace root.
3. If `agent-prompts/workspace-mode.md` is missing, unreadable, ambiguous, or internally inconsistent, stop and report the exact blocker.
4. Do not search parent directories.
5. Do not search child repositories for the workspace prompt.
6. Do not fall back to regular single-repo mode after `Mode: workspace` was requested.

The workspace prompt must define:

1. workspace root;
2. plan owner repo;
3. plan prompt root;
4. plan output path;
5. allowed target repos;
6. worktree path pattern for each target repo;
7. documentation boundary rules.

In workspace mode, the workspace root is orchestration context only. Git commands must run in the plan owner repo or declared target repo worktrees, never in the workspace root. Plans and internal documentation must be written only where the workspace prompt allows.

Workspace plans must include the ticket index table with a `Target repo` column:

```md
| Ticket | Target repo | Title | Dependencies | Independent group | Expected files/modules | Required verification |
| --- | --- | --- | --- | --- | --- | --- |
```

Each ticket must target exactly one allowed repo. If a change requires both repos, split it into dependent tickets. Do not create cross-repo tickets unless the workspace prompt explicitly defines a two-repo commit and recovery protocol.

Map plan kind to branch/worktree prefixes:

| Plan kind | Branch prefix | Worktree prefix |
| --- | --- | --- |
| feature | `feat` | `feat` |
| fix | `fix` | `fix` |
| improvement | `improve` | `improve` |

For plan slug `my-plan`:

- Branch: `[feat|fix|improve]/my-plan`
- Worktree: `../[feat|fix|improve]-my-plan`

In workspace mode, use the workspace prompt's repo-qualified worktree path pattern instead of the regular single-repo worktree path.

Choose a concise kebab-case slug yourself unless the user supplied one.

## Invocation Modes

Default any new `$ship-loop` invocation to planning handoff mode unless the user explicitly asks for direct brief mode or explicitly says to ignore prior conversation.

Regular single-repo mode is the default when `Mode: workspace` is absent. In regular mode, require `agent-prompts/plan-structure.md` and `agent-prompts/ticket-implementation.md` at the current repo root.

Workspace mode is selected only by:

```text
Mode: workspace
```

In workspace mode, require the workspace prompt at `agent-prompts/workspace-mode.md` in the invocation directory, then load the plan and ticket prompts from the configured plan prompt root.

### Planning Handoff Mode

Use this as the default for feature, fix, and improvement starts. The requirements source is the full current thread, not only the final invocation message.

Example:

```text
Use $ship-loop to finalize from this conversation.

Kind: feature
```

In this mode:

1. Treat the accumulated conversation as the planning context.
2. Preserve explicit decisions, rejected alternatives, constraints, edge cases, and authority docs from the thread.
3. Summarize accepted decisions before creating the worktree if the conversation was long or contained substantial debate.
4. Ask one concise question only if a material requirement remains unresolved.
5. Choose a concise kebab-case plan slug yourself unless the user supplied one.
6. Use the current checked-out branch/HEAD as the base unless the user explicitly supplies a different base.

### Explicit Direct Brief Mode

Use this only for narrow work where the user explicitly gives enough detail in one message and wants the direct brief to drive the plan.

Example:

```text
Use $ship-loop in direct brief mode.

Kind: fix
Brief: Fix authenticated sessions with malformed role claims so they fail hard at every application boundary.
```

In this mode, choose a concise kebab-case slug from the brief unless the user supplied one. Do not discard relevant prior conversation unless the user explicitly says to ignore it. If the brief is too thin to create a definitive implementation plan, ask one concise question instead of creating a vague plan.

## Workflow

### 1. Intake

Before creating anything, determine:

1. plan kind: `feature`, `fix`, or `improvement`;
2. mode: regular single-repo mode unless the user explicitly supplied `Mode: workspace`;
3. planning context, either the full current thread in planning handoff mode or the direct brief in direct brief mode;
4. plan slug, chosen by the agent as concise kebab-case unless the user supplied one;
5. base branch or commit for each repo that will receive a worktree, defaulting to each repo's current checked-out branch/HEAD;
6. plan path, read from the plan prompt or workspace prompt after the slug is chosen.

If plan kind or material planning context is missing and cannot be read directly from the current thread, ask one concise question. Do not ask for a slug unless the user wants to control naming.

### 2. Create Worktree And Branch

In regular single-repo mode, from the original repo root:

1. Read repository agent instructions, such as `AGENTS.md` when present.
2. Verify `agent-prompts/plan-structure.md` and `agent-prompts/ticket-implementation.md` exist.
3. Verify the branch does not already exist.
4. Verify the target sibling worktree path does not already exist.
5. Resolve the base immediately before worktree creation:
   - if on a branch, use the current branch name;
   - if detached, use the exact `HEAD` SHA and report that detached base in the run ledger.
6. Run `git worktree add ../[prefix]-[slug] -b [prefix]/[slug] [resolved-base]`.
7. Continue all plan and ticket work from the new worktree.

If branch or worktree already exists, stop unless the user explicitly invoked resume mode.

In workspace mode, from the workspace root:

1. Read `agent-prompts/workspace-mode.md`.
2. Verify the plan owner repo and every allowed target repo exists.
3. Verify the plan prompt root contains `plan-structure.md` and `ticket-implementation.md`.
4. Read repository agent instructions, such as `AGENTS.md` when present, from the plan owner repo and every target repo that may receive tickets.
5. Verify branches and repo-qualified worktree paths do not already exist.
6. Resolve the base immediately before each worktree creation:
   - if the target repo is on a branch, use the current branch name;
   - if detached, use the exact `HEAD` SHA and report that detached base in the run ledger.
7. Create a plan owner worktree using the workspace prompt's worktree path pattern.
8. Create target repo worktrees only when the parsed plan includes tickets for that repo.
9. Continue plan work from the plan owner worktree and ticket work from the ticket's target repo worktree.

If `Mode: workspace` was requested and any workspace requirement is unavailable, stop. Do not fall back to regular mode.

### 3. Create And Commit The Plan

In the new worktree:

1. Use subagents to inspect relevant codebase areas before writing the plan when required by repo instructions or useful for context.
2. Load `agent-prompts/plan-structure.md`.
3. Use the full planning context from the current thread, not only the final invocation message.
4. Create the plan at the repo-owned path.
5. Enforce exact ticket headings:

```md
### Ticket ABC-01 - Title
```

6. Require the ticket index table:

```md
| Ticket | Target repo | Title | Dependencies | Independent group | Expected files/modules | Required verification |
| --- | --- | --- | --- | --- | --- | --- |
```

7. Run `python3 /path/to/skill/scripts/extract_tickets.py [plan-path]`.
8. Reject any ticket whose `Target repo` is absent or names more than one repo. In workspace mode, also reject any ticket whose `Target repo` names a repo outside the workspace prompt's allowed target repos.
9. In workspace mode, create any target repo worktrees that were deferred until the ticket set was known.
10. Commit the plan only after ticket extraction succeeds.

Plan commit message:

```txt
[PREFIX] plan: [slug]
```

where `[PREFIX]` is `feat`, `fix`, or `improve`.
