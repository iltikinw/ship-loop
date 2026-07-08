# Final Review

## Final Review

After every ticket is committed:

1. Run the plan's required verification and at minimum `git diff --check`.
2. In workspace mode, run verification in every repo that received ticket commits and inspect `git diff --check` in each relevant worktree.
3. Run actual Codex CLI review agents through `scripts/run_final_review.py` for each repo worktree that received ticket commits and the plan owner repo when it differs. Use exact base SHAs resolved during worktree creation for review correctness, not movable branch names. Also pass the base branch name for PR creation with `--pr-base NAME=BRANCH`.

```bash
python3 /path/to/skill/scripts/run_final_review.py \
  --repo relay-monorepo=[relay-worktree] \
  --base relay-monorepo=[resolved-relay-base-sha] \
  --pr-base relay-monorepo=[relay-base-branch] \
  --repo pathways-admissions-visitlane=[pathways-worktree] \
  --base pathways-admissions-visitlane=[resolved-pathways-base-sha] \
  --pr-base pathways-admissions-visitlane=[pathways-base-branch] \
  --plan [plan-path] \
  --state-file [state-file] \
  --codex-bin [path-to-codex-bin]/codex \
  --publish-if-clean \
  --publish-remote origin \
  --stale-warning-heartbeats 2 \
  --stale-fail-heartbeats 8
```

If a repo was based on a detached commit and there is no unambiguous PR base branch, do not guess. Stop and ask for the PR base branch before publishing.

The helper rejects any final-review model or reasoning-effort override other than `gpt-5.5` and `high`. It runs `git diff --check [base-sha]...HEAD`, then invokes Codex CLI review agents with this required shape:

```bash
codex -a never -C [repo-worktree] -s read-only exec review \
  --base [resolved-base-sha] \
  -m gpt-5.5 \
  -c 'model_reasoning_effort="high"' \
  --output-schema /path/to/skill/schemas/review-result.schema.json \
  --output-last-message [durable-review-result-json] \
  --json
```

The helper writes raw review stdout/stderr to `[git-metadata]/ship-loop/[plan-slug]/logs/review/[repo].log` and the schema result to `[git-metadata]/ship-loop/[plan-slug]/logs/review/[repo].result.json` when `--state-file` is provided. It must not stream raw review transcripts to the parent by default.

If final review returns prose instead of schema JSON, normalize from the existing review log once or stop with the parser failure and log path. Do not rerun final review merely to recover structured output.

If final review hits a quota, rate-limit, retry-after, or usage-limit blocker, persist state, report the blocker and log path, and stop. Do not keep the parent thread alive waiting through a reset window.

4. If every repo review returns `status: pass` and `--publish-if-clean` is set, the helper must:
   - require the GitHub CLI `gh`;
   - require a clean worktree for every repo;
   - require the current HEAD to be on a branch;
   - run `git push -u [remote] [branch]` in each reviewed repo;
   - create a PR with `gh pr create --head [branch] --base [base-branch]`, or reuse an existing open PR for that head branch;
   - never merge a PR, enable auto-merge, delete branches, or push tags;
   - write publish logs under `[git-metadata]/ship-loop/[plan-slug]/logs/publish/[repo].log`.
5. If any final review fails, the helper must not push any branch or create any PR.
6. The parent agent must not replace the CLI review with a manual review.
7. The review must prioritize bugs, regressions, security/trust-boundary risks, missing tests, repository-instruction violations, and plan incompleteness.
8. `run_final_review.py` prints aggregate JSON with `reviews` and `pull_requests` before exiting. It exits nonzero when any repo review reports `status: fail` or when publishing fails after a clean review. The parent agent must pass review findings and PR links directly back to the user before any ledger or summary.
9. Do not fix review findings, create a repair ticket, rerun the loop, merge a PR, push tags, delete branches, or enable auto-merge unless the user explicitly asks for that follow-up.

Final report must lead with review results, then include a compact run ledger:

1. branch and worktree;
2. plan path and plan commit;
3. ticket commits grouped by repo;
4. tests/proofs run;
5. review findings exactly as returned by the review helper, grouped by repo;
6. PR links exactly as returned by the review helper, grouped by repo;
7. remaining gaps or risks;
8. final confidence score with the repository's required breakdown.
