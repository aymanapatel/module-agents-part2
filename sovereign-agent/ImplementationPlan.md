# Implementation Plan

## Scope

This plan targets the `sovereign-agent` repository: a Python 3.12+ framework and tutorial codebase for always-on AI agents built around session directories, filesystem IPC, tickets/manifests, explicit tool registries, and loop/structured halves.

The repository already contains a working alpha (`v0.2.0`) with tests, examples, chapters, CLI, and docs. The plan below focuses on moving the project from the current alpha state to a more complete, production-ready `v0.3`/`v1.0` baseline while preserving its teaching value.

## Current State Summary

### Implemented and exercised

- Session directory lifecycle and forward-only state: `sovereign_agent/session/`
- Session queue, scheduler, IPC watcher, tickets, manifests, and trace/report generation
- Planner/executor loop half with defensive JSON parsing and built-in tools
- CLI entry points: `version`, `doctor`, `run`, `serve`, `report`, `sessions`
- Worker abstractions with bare and subprocess backends: `sovereign_agent/orchestrator/worker.py`
- OS isolation policy wrappers for Landlock/sandbox-exec: `sovereign_agent/_internal/isolation.py`
- Verifier protocol and structured-half rule support: `sovereign_agent/halves/verifiers.py`, `sovereign_agent/halves/structured.py`
- Chapter/tutorial structure with drift verification: `chapters/`, `tools/verify_chapter_drift.py`
- End-to-end mocked planner/executor tests and example scenario tests

### Known gaps and skeletons

- Memory retrieval and consolidation are placeholders in `sovereign_agent/memory/__init__.py`.
- Structured half has a minimal rule engine but no default rule registry or richer dialog/state integration.
- Credential gateway only loads env vars; per-tool scoping and spawn-time injection are TODOs in `sovereign_agent/orchestrator/credentials.py`.
- Mount allowlist validation exists, but additional mount wiring into worker/container execution is not complete.
- Docker worker spawning is referenced in docs/changelog but not implemented.
- Voice pipeline is protocol-only with `SpeechmaticsVoicePipeline` stubs.
- Observability has JSONL trace/report support, while Evidently/OpenTelemetry-style backends remain import-gated/scaffolded.
- The architecture docs point to root `SOW.md`, but that file is not present locally.

## Implementation Principles

1. **Filesystem remains the source of truth.** New features must persist state through the session directory, not in process memory.
2. **Fail closed for safety boundaries.** Isolation, credential scoping, mount validation, and approval flows should deny by default.
3. **Every long operation gets a ticket and manifest.** Preserve the ticket lifecycle and SHA-256 manifest discipline.
4. **Keep the tutorial and library in sync.** Any production-module change that maps to a chapter must update the corresponding chapter solution/starter/tests and pass drift verification.
5. **Fake clients first, real LLM second.** Add deterministic unit/integration tests before real-provider smoke tests.
6. **Public API stability.** Preserve `sovereign_agent.__all__` semantics for `0.2.x`; document breaking changes for `0.3.0`.

## Phase 0 — Baseline and Spec Alignment

**Goal:** establish a clean starting point and make the local spec complete.

### Tasks

- Run baseline checks:
  - `make lint`
  - `make test`
  - `make drift`
  - `make preflight`
- Restore or replace the missing root `SOW.md` referenced by `docs/architecture.md`.
- Reconcile README/test-count drift (`README.md`, `Makefile`, `CHANGELOG.md` mention different counts).
- Create a lightweight roadmap section in `CHANGELOG.md` for `v0.3` targets.
- Confirm all examples listed in README actually exist or update docs to match the current tree.

### Acceptance Criteria

- Baseline checks pass locally.
- `docs/architecture.md` links to an existing local source of truth.
- README, Makefile, docs, and changelog agree on capabilities and test counts.

## Phase 1 — Memory Subsystem

**Goal:** replace placeholder memory behavior with useful, testable retrieval and consolidation.

### Tasks

- Extend `MemoryEntry` metadata handling with stable timestamps, tags, source, score, and optional embedding metadata.
- Implement deterministic keyword/metadata retrieval as the first backend:
  - query token matching
  - type filters
  - tag filters
  - recency ordering
  - stable scoring returned in metadata or a retrieval result wrapper
- Add optional embedding-backed retrieval:
  - pluggable embedding client interface
  - local embedding cache under `session/memory/cache/`
  - cache invalidation by content hash
  - fallback to deterministic retrieval when embeddings are unavailable
- Implement consolidation:
  - summarize non-empty working memory into semantic/episodic facts
  - preserve source references and timestamps
  - clear working memory only after successful fact write and index update
- Add tests in `tests/unit/` for store, retrieval filters, scoring, cache behavior, and consolidation idempotency.
- Add an integration test that runs a loop, writes memory, retrieves it in a later step, and verifies trace events.

### Acceptance Criteria

- `MemoryRetrieval.retrieve()` no longer returns simple append-order placeholders.
- Consolidation is idempotent and atomic.
- Memory events appear in `logs/trace.jsonl` without making trace failures fatal.
- Tests cover no-LLM and fake-client paths.

## Phase 2 — Worker Backends, Mounts, and Credential Gateway

**Goal:** make worker execution configurable, scoped, and auditable.

### Tasks

- Add explicit worker backend config, for example:
  - `worker_backend: Literal["bare", "subprocess", "docker"]`
  - `worker_timeout_s`
  - `worker_allow_network`
  - `worker_isolation: Literal["auto", "none", "landlock", "sandbox-exec"]`
- Wire `Orchestrator.process_session()` through a selected `WorkerBackend` instead of directly calling dispatch helpers for long-running operation mode.
- Keep `run_task()` deterministic and test-friendly by defaulting to bare/fake-client behavior where appropriate.
- Implement credential scoping:
  - load allowlist from `~/.config/sovereign-agent/tool-credentials.json`
  - map tool names to env var names
  - inject only scoped env vars into subprocess/Docker workers
  - write audit trace events with key names only, never values
- Complete mount allowlist integration:
  - validate every requested extra mount with `require_mount()`
  - mount non-main roots read-only unless explicitly allowed
  - reject blocked path components by default
- Implement `DockerWorker` if Docker remains in scope:
  - use the same `worker_entrypoint`
  - bind session directory read-write
  - bind runtime/project dependencies read-only
  - apply credential and mount policies
  - collect stdout/stderr tails into `WorkerOutcome.raw`
- Add unit tests for backend selection, credential allowlist parsing, denied mounts, and audit events.
- Add integration tests for subprocess backend; mark Docker tests as optional/slow if daemon-dependent.

### Acceptance Criteria

- Orchestrator can run a session through bare and subprocess backends via config.
- Workers receive only allowed credentials.
- Additional mounts are denied unless under allowlisted roots.
- Safety-relevant denials are covered by tests and emit useful error messages.

## Phase 3 — Structured Half Integration

**Goal:** make the structured half usable as a first-class runtime component, not only a standalone rule list.

### Tasks

- Define a structured-rule registration API:
  - programmatic registration for applications
  - optional config/entry-point discovery for packages
- Update orchestrator structured dispatch to load registered rules instead of always escalating.
- Persist structured-half decisions to trace and tickets, including verifier reason/score.
- Add support for async rule actions if needed by real workflows.
- Define handoff payload schema between loop and structured halves.
- Add tests for:
  - matching rule completes a handed-off session
  - no matching rule escalates
  - verifier-driven escalation persists reason and score
  - malformed handoff payload fails safely
- Add or update example scenarios that demonstrate loop-to-structured handoff.

### Acceptance Criteria

- `handed_off_to_structured` sessions can complete through registered rules.
- Structured decisions are auditable in tickets and traces.
- Existing `StructuredHalf` public API remains backward compatible.

## Phase 4 — Observability and Dataflow Integrity

**Goal:** strengthen “it ran” vs “it worked” verification across examples and reports.

### Tasks

- Formalize a scenario-level dataflow audit interface.
- Add audit outputs to session artifacts, ideally under `workspace/audits/` or `logs/`.
- Extend `generate_session_report()` to include:
  - dataflow audit status
  - ticket/manifest failures
  - worker backend and isolation mode
  - credential injection audit summary without secrets
- Flesh out optional observability backends:
  - keep JSONL as canonical local backend
  - implement Evidently/OpenTelemetry adapters behind extras only if they do not complicate the core path
- Add tests that intentionally fabricate an output and verify the audit catches it.

### Acceptance Criteria

- Every example scenario has a dataflow integrity audit.
- Reports distinguish structural success from dataflow success.
- Optional observability imports fail with clear install-extra messages.

## Phase 5 — Voice Pipeline and Optional Extensions

**Goal:** turn protocol stubs into opt-in, dependency-gated extensions without burdening core installs.

### Tasks

- Decide whether voice belongs in core `v0.3` or a later lesson package.
- If in scope, implement `SpeechmaticsVoicePipeline.listen()` and `.speak()` behind `[voice]` extra.
- Write audio artifacts under `session/extras/voice/` or a documented equivalent.
- Add fake ASR/TTS implementations for tests.
- Add clear `requires_extra()` errors for missing optional dependencies.
- Document provider credentials and data retention implications.

### Acceptance Criteria

- Core installation remains lightweight.
- Voice tests run without real provider calls.
- Real-provider smoke tests are opt-in and token/cost aware.

## Phase 6 — Docs, Lessons, and Tutorial Drift

**Goal:** keep the repository useful both as a library and a curriculum.

### Tasks

- Add docs for:
  - memory API and retrieval behavior
  - worker backend configuration
  - credential scoping
  - structured-half registration
  - dataflow audit interface
- Publish at least one lesson that replaces a current skeleton, likely memory retrieval/consolidation.
- Update chapter READMEs/tests when production modules change.
- Keep `tools/verify_chapter_drift.py` passing after every mapped production change.
- Add a “feature maturity” table to docs/API or README: stable, alpha, skeleton, deferred.

### Acceptance Criteria

- New users can follow `make first-run` and then run one chapter demo and one example scenario successfully.
- Docs accurately label skeletons versus production-ready APIs.
- Drift check remains part of the default PR/CI path.

## Phase 7 — Release Hardening

**Goal:** prepare a reliable release candidate.

### Tasks

- Run local release checks:
  - `make ci`
  - `make preflight`
  - `make pre-publish`
  - `make ready-to-ship`
- Run opt-in real LLM checks when credentials are available:
  - `make ci-real-estimate`
  - `make ci-real-quick`
  - selected `*-real` examples
- Review public API exports and semver notes.
- Confirm no secrets, sessions, generated artifacts, or local allowlists are included in package data.
- Update changelog with implemented features, breaking changes, migration notes, and known limitations.

### Acceptance Criteria

- All deterministic checks pass.
- Real-provider failures, if any, are documented separately from deterministic suite status.
- Release notes distinguish stable features from optional/skeleton extensions.

## Suggested Work Order

1. **Spec/docs alignment**: restore `SOW.md` or fix links, normalize status claims.
2. **Memory**: high user value, currently the clearest placeholder, testable without external services.
3. **Worker/credentials/mounts**: safety boundary; should land before promoting production claims.
4. **Structured-half orchestration**: connects existing verifier/rule work to the runtime.
5. **Dataflow audits and reporting**: makes example success meaningful.
6. **Optional extensions**: voice, Docker, Evidently/OTel as extras once core safety is solid.
7. **Docs and release**: update curriculum and release notes continuously, not only at the end.

## Testing Strategy

- Prefer deterministic tests using `FakeLLMClient` and fake providers.
- Add unit tests next to each subsystem before integration tests.
- Use integration tests for session-directory contracts: files, state transitions, tickets, manifests, traces.
- Mark external-service tests as `network` or `slow` and keep them out of default local runs.
- Run chapter drift verification whenever production modules mirrored by chapters change.

Recommended routine checks:

```bash
make lint
make test
make drift
make preflight
```

Before release:

```bash
make ci
make pre-publish
make ready-to-ship
```

## Key Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Credential leakage into workers | Deny by default, per-tool allowlists, audit key names only, tests for absence of unrelated env vars |
| Mount escape or secret exposure | Validate resolved paths, merge default blocked patterns, read-only non-main mounts, fail closed |
| Memory retrieval becomes nondeterministic | Keep deterministic retrieval fallback and fake embedding clients for tests |
| Structured half becomes a hidden workflow engine | Keep default small; make richer integrations pluggable and explicit |
| Docs overstate skeleton features | Maintain feature maturity table and update README/changelog with every release |
| Chapter/tutorial drift | Keep drift verification in CI and update chapter solutions/starter tests with production changes |

## Definition of Done for the Next Milestone

- Memory retrieval/consolidation are implemented beyond placeholders and covered by tests.
- Orchestrator can select and use worker backends through config.
- Credential scoping and mount validation are enforced for isolated workers.
- Structured handoffs can be completed by registered rules.
- Session reports include dataflow audit status.
- Docs identify stable APIs, alpha APIs, skeletons, and deferred work.
- `make ci` and `make drift` pass locally.
