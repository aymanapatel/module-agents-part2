# Complete Course Chapter Assignment

## Summary

Complete the repo as a chapter-based course assignment by implementing the `starter.py` exercises for Chapters 1-5, validating each chapter against its tests and demos, and running the repo-level checks needed for a clean handoff. Do not change `.env` or expose secrets.

## Key Changes

- Implement Chapter 1 session primitives in `chapters/chapter_01_session/starter.py`:
  - traversal-safe `Session.path`
  - forward-only `update_state`
  - atomic trace append
  - `create_session`, `load_session`, `list_sessions`
- Implement Chapter 2 `SessionQueue` in `chapters/chapter_02_queue/starter.py`:
  - per-session serialization
  - global concurrency cap
  - retry with exponential backoff
  - `_close` idle preemption
  - graceful shutdown without cancelling active work
- Implement Chapter 3 starter primitives:
  - atomic JSON IPC write
  - manifest verification by SHA-256
  - ticket success/failure behavior matching production modules
- Implement Chapter 4 `compute_next_run`:
  - once, interval, and cron behavior
  - interval anchoring to scheduled time
  - skip-ahead for missed intervals
- Implement Chapter 5 starter helpers:
  - defensive planner JSON parsing
  - optional minimal ReAct loop step if required by assignment tests
- Keep `solution.py` files as reference re-exports from production code. Do not replace production modules unless a starter test reveals a real shared bug.

## Test Plan

- For each chapter, temporarily switch that chapter's `tests.py` import from `solution` to `starter` while validating the student implementation.
- Run focused chapter tests:
  - `uv run pytest chapters/chapter_01_session/tests.py -v`
  - `uv run pytest chapters/chapter_02_queue/tests.py -v`
  - `uv run pytest chapters/chapter_03_ipc/tests.py -v`
  - `uv run pytest chapters/chapter_04_scheduler/tests.py -v`
  - `uv run pytest chapters/chapter_05_planner_executor/tests.py -v`
- Run demos:
  - `make demo-ch1`
  - `make demo-ch2`
  - `make demo-ch3`
  - `make demo-ch4`
  - `make demo-ch5`
- Run final repo checks:
  - `make lint`
  - `make test`
  - `make drift`
  - `make preflight`
- If a real Nebius key is configured, run:
  - `make verify`
  - `make demo-ch5-real`

## Assumptions

- "Assignment" means completing the course chapter starters in this repo, not the broader v0.3/v1.0 roadmap.
- The production modules under `sovereign_agent/` and each chapter's `solution.py` are authoritative references.
- Real LLM checks are optional because they require a valid `.env` API key and network access.
- Generated session/demo artifacts may be created during validation, but tracked source files should only change where starter/test validation requires it.
