from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .governance import (
    BackgroundMemoryPipeline,
    ConsolidationResult,
    MemoryCandidate,
    MemoryDecision,
)
from .durable_governance import DurableMemoryGovernanceEngine
from .retrieval import MemoryRetriever
from .store import MemoryStore
from .candidate_extractor import MemoryCandidateExtractor


class MemoryManager:
    def __init__(self, store: MemoryStore, *, project_root: Path) -> None:
        self.store = store
        self.retriever = MemoryRetriever(store)
        self.background = BackgroundMemoryPipeline()
        project_key = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:20]
        self.namespace = f"stock-ai-project:{project_key}"
        self.governance = DurableMemoryGovernanceEngine(
            store,
            namespace=self.namespace,
        )
        self.candidate_extractor = MemoryCandidateExtractor()

    def retrieve(
        self,
        objective: str,
        *,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.retriever.relevant(
            namespace=self.namespace,
            objective=objective,
            session_id=session_id,
            limit=limit,
        )

    def consider(self, candidate: MemoryCandidate) -> ConsolidationResult:
        """Evaluate a candidate before any durable persistence is attempted."""

        return self.governance.consider(candidate)

    def remember_run(
        self,
        *,
        session_id: str,
        run_id: str,
        objective: str,
        summary: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        source = {"type": "agent_run", "run_id": run_id, "evidence": evidence}
        episodic: dict[str, Any] = {}
        consolidation: ConsolidationResult | None = None
        if not _contains_transient_market_measurement(objective, summary):
            candidate = MemoryCandidate(
                content=f"Objective: {objective}\nVerified result: {summary}",
                kind="episodic",
                importance=0.8,
                future_relevance=0.7,
                confidence=0.95 if evidence else 0.75,
                source=source,
                durability="long_term",
                semantic_key=f"run:{run_id}",
                run_id=run_id,
                session_id=session_id,
                host_verified=bool(evidence),
            )
            consolidation = self.consider(candidate)
            if consolidation.decision in {
                MemoryDecision.SAVE,
                MemoryDecision.MERGE,
                MemoryDecision.SUPERSEDE,
            } and consolidation.record:
                episodic = self.store.get(consolidation.record.memory_id) or {}
        working = self.store.replace_session_working(
            namespace=self.namespace,
            session_id=session_id,
            run_id=run_id,
            content=(
                "Current session summary\n"
                f"Most recent objective: {objective}\n"
                f"Most recent verified result: {summary}"
            ),
            source=source,
        )
        extracted = self.candidate_extractor.extract(
            objective=objective,
            summary=summary,
            evidence=evidence,
            session_id=session_id,
            run_id=run_id,
        )
        extracted_results: list[dict[str, Any]] = []
        for candidate in extracted:
            decision = self.consider(candidate)
            extracted_results.append({
                "candidate_id": decision.candidate_id,
                "decision": decision.decision.value,
                "reason": decision.reason,
            })
        return {
            **episodic,
            "working_memory_id": working.get("memory_id"),
            "consolidation_decision": (
                consolidation.decision.value if consolidation else "working_only_transient_market_data"
            ),
            "candidate_id": consolidation.candidate_id if consolidation else None,
            "extracted_candidates": extracted_results,
        }


def _contains_transient_market_measurement(objective: str, summary: str) -> bool:
    text = f"{objective}\n{summary}".casefold()
    market_context = any(
        token in text
        for token in (
            "股票", "股價", "行情", "收盤", "成交量", "漲幅", "跌幅",
            "price", "quote", "market", "volume", "rsi", ".tw", ".two",
        )
    )
    measurement = bool(
        re.search(r"[$＄]\s*\d|[+\-]?\d+(?:\.\d+)?\s*[%％]|\b\d+(?:\.\d+)?\s*(?:元|美元|股)\b", text)
    )
    return market_context and measurement
