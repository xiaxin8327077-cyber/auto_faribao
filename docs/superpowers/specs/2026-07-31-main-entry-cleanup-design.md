# Main Entry Cleanup Design

## Problem

The repository tracks both `main.py` and `src/main.py`. They began as identical
copies, but only the root `main.py` is documented and used by the production
startup command. Recent runtime wiring was committed only to `src/main.py`,
while the corresponding root-file update remained uncommitted. Maintaining two
independent entry implementations can therefore omit production changes.

## Decision

- Keep root `main.py` as the only application entry point.
- Delete `src/main.py`; no code, test, script, package, or deployment reference
  uses it.
- Preserve and commit the current portfolio-runtime wiring in root `main.py`.
- Add a repository-structure regression test that requires the root entry point
  and rejects a second `src/main.py` implementation.
- Do not modify or stage the other existing uncommitted workspace changes.

## Verification

1. Run the new structure test and confirm it fails before deleting
   `src/main.py`.
2. Delete `src/main.py` and confirm the structure test passes.
3. Run the startup/runtime-focused tests.
4. Run the full test suite and `git diff --check`.

