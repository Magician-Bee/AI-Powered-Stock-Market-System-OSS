from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class ModelInvocationReceipt(BaseModel):
    call_id: str
    provider: str
    model_id: str
    status: Literal["succeeded", "failed", "not_run"]
    started_at: str
    completed_at: str
    error_type: str | None = None
    error_message: str | None = None
    raw_output_preserved: bool = False


class AnalysisProvenance(BaseModel):
    origin: Literal["model", "rule_strategy", "hybrid", "none"]
    provider: str | None = None
    model_id: str | None = None
    model_call_id: str | None = None
    model_call_succeeded: bool = False
    rule_set_id: str | None = None
    universe_source: str = "none"
    symbols_considered: list[str] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    data_ready: bool = False
    fallback_used: bool = False
    fallback_reason: str | None = None
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ConfidenceValue(BaseModel):
    value: float | None = None
    type: Literal[
        "calibrated_probability",
        "model_self_reported",
        "rule_score",
        "ranking_score",
        "data_quality_score",
        "none",
    ] = "none"
    scale_min: float | None = None
    scale_max: float | None = None
    calibrated: bool = False
    calibration_method: str | None = None


class DecisionEnvelope(BaseModel):
    observation: dict[str, Any] = Field(default_factory=dict)
    rule_analysis: dict[str, Any] | None = None
    model_analysis: dict[str, Any] | None = None
    risk_evaluation: dict[str, Any] | None = None
    execution_status: dict[str, Any] | None = None
    provenance: AnalysisProvenance
    model_invocation: ModelInvocationReceipt | None = None
