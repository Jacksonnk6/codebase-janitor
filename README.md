# janitor

A tiny codebase janitor. It finds the junk nobody has time to clean up, fixes the safe stuff on its own git branch, and rolls everything back if your checks fail.

One file, no dependencies, Python 3.8+.

## Usage

```bash
# Report only (exit code 1 if anything is found, so it works in CI)
python janitor.py path/to/repo

# Fix on a fresh branch and commit
python janitor.py path/to/repo --fix

# Also bump patch/minor pins in requirements.txt
python janitor.py path/to/repo --fix --bump-deps

# The safety net: run your tests before committing.
# If they fail, the branch is deleted and the repo is untouched.
python janitor.py path/to/repo --fix --bump-deps --verify "python -m pytest -q"
```

Flags: `--offline` skips the PyPI lookup.

## What it does

| Check | Action |
|---|---|
| Unused imports | Auto-fixed (handles `import a, b` and `from x import a, b` partially unused) |
| Probably-dead functions/classes | Reported only. A human decides. |
| Outdated pinned deps | Patch/minor auto-bumped with `--bump-deps`. Major bumps flagged for a human. |

Safety rules baked in:
- `--fix` refuses to run outside a git repo or on a dirty working tree
- every run happens on its own `janitor/cleanup-<timestamp>` branch
- `--verify` failure means full rollback and branch deletion

## Known limits (it's an MVP)

- Python only; `requirements.txt` with `==` pins only
- Dead-code detection is name-based: it can miss dead code that shares a name with something live, and it skips decorated functions (routes, CLI commands) to avoid false positives
- Only module-level imports are checked; string annotations are not parsed
- Rewriting a partly-unused import drops any trailing comment on that line

## Roadmap to a real product

1. **Open PRs** instead of local branches (GitHub App), with a readable summary per change
2. **More languages**: TypeScript/JS first (package.json + ts-prune style checks)
3. **Migration recipes**: framework and major-version upgrades driven by an LLM, gated by the same verify-or-rollback loop
4. **Scheduling**: weekly sweeps, one small PR at a time so reviewers don't drown
5. **Trust dashboard**: merge rate, revert rate, and lines removed per repo. This is the metric that sells it.

The moat is the verify-or-rollback loop: teams will let an agent touch their code only if a bad change can never land.
