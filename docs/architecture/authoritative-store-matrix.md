# Authoritative Store Matrix

`config/authoritative_store_matrix.yaml` is the machine-readable ownership
map for durable domain state. A class or table existing in the repository does
not make it a source of truth.

Every domain has exactly one named authoritative store. A projection must be
explicitly listed with `read_only: true`; it may render UI or answer queries,
but it cannot become a second writable truth. The loader validates duplicate
ownership, missing modules, projection writeability, and owner/projection
collisions during capability-status construction.

`live_order` is canonical: `BrokerOMSStore` owns every durable intent,
idempotency, broker-order and report binding. `BrokerOrderManagementGateway`
is only the safety-coordinating runtime facade; when a durable store is
configured it refreshes reads from that store and persists all transitions
through it, so process-local mappings cannot become a second truth.
`forest` is canonical: `DurableForestStore` owns the durable Forest tables and
is the only runtime writer used by `FinalAgentRuntime`; `DurableForestProjector`
remains a compatibility alias for older integrations.
`artifact` is canonical: `ArtifactStore` owns both the durable artifact
record and its immutable version-1 lineage, so a production writer cannot
make an artifact visible before its content-addressed history exists.
`research_experiment` now has a dedicated `ExperimentStore`; its immutable
table remains in the shared SQLite database, but the model registry and legacy
compatibility facade both route through that one authority. Transitional
entries document the intended owner and concrete migrations; they are not
production-complete evidence. Live broker activation remains disabled until
the separate OMS, reconciliation, broker and capability-promotion requirements
are accepted.

Runtime consumers can call `authoritative_store_for(domain_id)` or
`is_authoritative_store(...)` when binding a writer. The complete matrix and
content hash are exposed by capability status for audit and UI inspection.
