# Stock AI Agent Runtime contract

## Product identity and ownership

The product Agent is always **Stock AI Agent**. Codex is the default model provider; OpenAI-compatible model APIs and external Agent Framework endpoints are also real, selectable providers. A provider supplies structured planning turns but does not own sessions, tools, permissions, state, memory, schedules, UI, or execution. User tasks enter through `/api/agents` and are not forwarded to a visible Codex chat.

The Host owns:

- `Session → Run → PlanGraph → Step → ToolCall → Validation → Event`
- environment snapshots, workflow templates, checkpoints, artifacts, memory and schedules
- Capability Registry routing and every actual side effect
- RiskEngine, Paper Broker, project boundaries and user approval
- worker lifecycle, cancellation, recovery and stream replay

Each configured model backend owns only its calls through the provider-neutral `ModelProvider` contract. `codex`, `openai-compatible` and `external-agent` are executable drivers built from the same Provider Registry. Local models use an OpenAI-compatible gateway; Anthropic and Gemini use a compatible gateway or external Agent adapter. Unconfigured providers are visible as unavailable and cannot be selected for a Run.

## Durable records

SQLite schema version 9 stores sessions, messages, runs, events, plans and revisions, checkpoints, approvals, memory, artifacts, workflows, schedules, scheduler leases, a durable host-event inbox, workers, control messages, durable UI state/commands and non-secret provider metadata. When SQLite supports FTS5, a trigram-backed memory index is created and maintained automatically. The database lives in:

```text
~/Library/Application Support/Stock AI/agent-runtime.db
```

Runtime directories use mode `0700`; artifacts, backups, browser state and the supervisor socket use user-only access. Database startup performs a bounded, fail-closed open/schema/migration-catalog check so a multi-gigabyte database cannot block the desktop readiness deadline; the full-page SQLite `quick_check` remains an explicit maintenance operation. Online backups are stored under `Application Support/Stock AI/backups`. Each explicit maintenance backup is verified before retention is applied, and keeps the fresh backup plus its immediate predecessor. Checkpoint compaction is limited to successfully completed Runs: their newest recoverable checkpoint remains intact while older cumulative copies in `checkpoint.created` become hash-bearing audit references. Failed, cancelled, interrupted, waiting, active and bounded partial Runs remain byte-for-byte untouched. The maintenance transaction verifies that Run and event row counts and all protected checkpoint rows are unchanged before commit. A maintenance `VACUUM` then checkpoints and truncates WAL frames, without interrupting a busy reader. Provider secrets use macOS Keychain or backend environment variables and are never written to SQLite, YAML, preference JSON, events or logs.

## Planning and execution

The model returns a provider-neutral PlanPatch. PlanGraph nodes may be reasoning, tool, subtask, subagent, approval, schedule, workflow, validation, checkpoint or finalize. The Host dispatches each ready node type rather than treating non-tool nodes as labels. Pending nodes may be added, changed, removed or reordered. Running and completed nodes are immutable.

Before execution, the Host Plan Compiler checks:

- dependencies and cycles
- capability existence
- input schema
- autonomy permissions
- mutation postconditions
- completion criteria

Capabilities publish input/output schemas, risk class, permissions, resource scope, timeout, retry policy, idempotency, side effects, preconditions, postconditions, validator, rollback support, execution backend and visible event policy. The registry validates arguments before calling a provider.

Long-running project, terminal, browser and external operations are supervised as cancellable workers. Each type publishes its execution boundary (`host_project_scope`, sandbox subprocess, isolated Playwright profile or external adapter process boundary), timeout and cancellation contract in the durable worker record. Stale running workers become `crashed` after restart, and health is available over a private Unix domain socket.

## Validation, approval and recovery

A model response cannot mark a tool successful. The Host validates result type, output schema, semantic status, tool-specific mutation evidence and declared postconditions. Generic `success: true` is insufficient: paper orders must match a persisted broker/account order, notifications must match every requested delivery receipt, project changes must have the expected hash transition, external workflows must identify a source-locked module hash, and schedules/UI/memory/artifacts must return their own durable identifiers or acknowledgements. A dated market measurement must be bound to one atomic Host fact record containing the same reporting period and values; matching isolated numbers across multiple rows is not evidence. Python, JSON, TOML and YAML project candidates are parsed before atomic replacement. Completion is accepted only when the plan is finished and required evidence exists. Stable general knowledge may complete without external evidence; current, niche, project, UI and trading tasks still require Host-validated trace evidence.

Policy is evaluated from the requested action's risk class and runtime scope, never from the model identity. Mutations are bound to an approval record containing run, step, tool, canonical argument digest, resource scope, risk class and expiry. A model cannot approve its own action; changed arguments require a new approval; consumed approvals cannot be replayed. A denial is recorded as control evidence and resumes the Run from a safe checkpoint for replanning instead of falsely treating the task as terminal.

Checkpoints contain plan revision, event sequence, transcript, tool trace and serializable run state. Restart changes active runs to `suspended` and resumes from the latest safe checkpoint. Completed call IDs are reused instead of executed again; argument-idempotent actions such as paper submission are also deduplicated. Project writes return before/after hashes, a unified diff and a run-scoped rollback token; a postcondition failure automatically invokes that host-issued token and records the rollback result.

## Durable scheduler event inbox

Time, interval and cron triggers are claimed directly from the schedule table. Host market quote, market event, news, portfolio and UI state/command events enter `agent_runtime_events` first. Publication assigns a content/time-window deduplication key; the Runtime claims events with an expiring lease, evaluates event and condition schedules, and marks each event processed only after dispatch completes. A crash releases the lease by expiry, so the next Runtime can retry without relying on an open browser or in-memory callback. Every triggered run remains advisory-only.

## Memory and live state

Memory is namespaced to the current Stock AI project and records source provenance. Conversation, working, project, domain, episodic, preference, artifact and reflection memory are supported. Retrieval is session-aware, uses FTS/trigram candidates when available and replaces the current working summary instead of accumulating stale summaries. Live market state requires expiry and expired memory is excluded. Current quotes, positions, account state and UI state always come from a fresh Environment Snapshot, never from memory.

## UI event contract

The UI reconnects through SSE using an event sequence. It shows auditable summaries, not private chain-of-thought:

- run/session/snapshot state
- proposed, revised and compiled plans
- selected Provider lifecycle, including Codex SDK lifecycle when Codex is active
- tool names, redacted arguments, Skills, packages and schedules
- validations, approvals, checkpoints, recovery and rollback
- results, errors, artifacts and memory writes

Sensitive keys such as passwords, secrets, tokens, API keys and authorization fields are redacted recursively before an event is stored or streamed.

## Immutable safety boundaries

- live brokerage is unavailable
- paper orders require explicit paper execution mode, matching preview, central risk approval and idempotency
- project and external mutations require matching autonomy plus exact user approval
- unconfigured or unknown model providers cannot be selected for a Run
- `/api/codex/run` is a separate developer diagnostic path, disabled by default, and never used by Stock AI Agent
