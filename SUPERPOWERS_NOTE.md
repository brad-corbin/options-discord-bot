# Superpowers note

Superpowers (or any agent harness wrapping Claude Code) needs to know a
few things up front so it doesn't fight the existing project conventions.

If you're configuring Superpowers / a system prompt layer for this
project, include these notes:

---

## Project context layer

Brad is a non-developer running an options-trading Python bot on Render.
Deploys go via GitHub Desktop. Trading is real money. Friday is shakedown
day for any new code. Don't hotfix during market hours.

The repo has a CLAUDE.md at the root that is the canonical project
briefing. Read it before any code session. It contains vocabulary,
architectural conventions, audit discipline, and current state.

## Working style

- Brad is not a developer. Skip the lecture. Give him the answer.
- When QA'ing, actually run the tests. "Tests would check..." is not
  the same as "tests passed."
- When something is done, say it's done. Don't invent more work.
- If Brad pushes back, default to agreeing and fixing — don't defend
  the original choice unless he asked for justification. He's caught
  several real bugs this way; the discipline of taking pushback
  seriously is valuable.
- Real file deliverables go straight into the repo. Don't paste full
  files in chat unless asked.

## Audit rules (the non-negotiables from CLAUDE.md)

1. Never inline a helper for a concept that has existing
   implementations. Grep first; if it exists, write a `canonical_X`
   wrapper.
2. AST-check every Python file after editing.
3. Don't bundle patches.
4. Wrapper-consistency tests are mandatory for canonicals.
5. Run the tests — don't just describe them.

## Tools that matter

- `python3 -c "import ast; ast.parse(open('PATH').read())"` — AST sanity
- `git status` / `git diff` — confirm what changed before committing
- `python3 test_*.py` — five canonical-rebuild test suites
- The deploy is `git push` (GitHub Desktop), Render auto-rebuilds. Don't
  invent infrastructure that doesn't exist.

## What NOT to do

- Don't propose architecture changes in the first session. Read first,
  verify, then engage.
- Don't write parallel implementations of concepts that already have
  canonicals.
- Don't run the live bot in the agent environment — there's no Schwab
  credentials there. Tests run against synthetic chains.
- Don't auto-commit or auto-push. Brad reviews and commits via GitHub
  Desktop.
