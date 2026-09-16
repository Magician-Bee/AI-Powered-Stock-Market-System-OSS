from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .workflow_compiler import CompiledWorkflow, WorkflowCompiler


@dataclass(frozen=True, slots=True)
class BackendResult:
    backend: str
    mode: str
    accepted: bool
    backend_reference: str | None
    detail: Mapping[str, Any]


class InternalSchedulerBackend:
    def __init__(
        self,
        submit: Callable[..., Mapping[str, Any]] | None = None,
        *,
        trigger_callback: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.submit = submit
        self.trigger_callback = trigger_callback
        self.activated: dict[str, Mapping[str, Any]] = {}

    def bind_trigger_callback(
        self,
        callback: Callable[[str, Mapping[str, Any]], Any] | None,
    ) -> None:
        self.trigger_callback = callback

    def dry_run(self, workflow: CompiledWorkflow) -> BackendResult:
        self._require(workflow)
        if self.submit is None:
            return BackendResult(
                self.backend_id,
                "dry_run",
                True,
                None,
                {"validated_locally": True, "scheduler_contacted": False},
            )
        detail = dict(self._submit(workflow.definition, True, {}))
        accepted = bool(detail.get("accepted", detail.get("would_schedule", False)))
        return BackendResult(self.backend_id, "dry_run", accepted, None, detail)

    def activate(
        self,
        workflow: CompiledWorkflow,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> BackendResult:
        self._require(workflow)
        if self.submit is None:
            return BackendResult(
                self.backend_id,
                "activate",
                False,
                None,
                {
                    "submission_status": "pending",
                    "durable_submission_required": True,
                    "reason": "internal scheduler submit callback is not configured",
                },
            )
        detail = dict(self._submit(workflow.definition, False, dict(context or {})))
        accepted = bool(detail.get("accepted", detail.get("scheduled", False)))
        reference_value = detail.get("schedule_id") or detail.get("backend_reference")
        if accepted and not reference_value:
            accepted = False
            detail = {**detail, "reason": "scheduler accepted submission without a durable reference"}
        receipt_value = detail.get("receipt_id") or detail.get("registration_receipt_id")
        durability = str(detail.get("durability") or "")
        if accepted and (
            not receipt_value
            or durability not in {"sqlite_schedule_registry", "external_durable_scheduler"}
        ):
            accepted = False
            detail = {
                **detail,
                "reason": "scheduler registration has no durable receipt",
                "durable_registration_required": True,
            }
        reference = str(reference_value) if accepted and reference_value else None
        if accepted and reference:
            self.activated[reference] = workflow.definition
        return BackendResult(self.backend_id, "activate", accepted, reference, detail)

    def dispatch(self, schedule_id: str, event: Mapping[str, Any]) -> Any:
        """Receive a scheduler callback and hand it to the bound controller."""

        if self.trigger_callback is None:
            raise RuntimeError("internal scheduler trigger callback is not configured")
        return self.trigger_callback(schedule_id, dict(event))

    @property
    def backend_id(self) -> str:
        return "internal_scheduler"

    def status(self) -> Mapping[str, Any]:
        return {
            "backend": self.backend_id,
            "configured": self.submit is not None,
            "activation_ready": self.submit is not None,
            "user_message": (
                "內部排程器已連接。" if self.submit is not None
                else "內部排程器尚未連接；不會假裝已啟用。"
            ),
        }

    def _require(self, workflow: CompiledWorkflow) -> None:
        WorkflowCompiler.require_compiler_product(workflow)
        if workflow.backend != self.backend_id:
            raise ValueError("workflow is not compiled for Internal Scheduler")

    def _submit(
        self,
        definition: Mapping[str, Any],
        dry_run: bool,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self.submit is None:
            raise RuntimeError("internal scheduler submit callback is not configured")
        try:
            signature = inspect.signature(self.submit)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
            ]
            has_varargs = any(
                parameter.kind is parameter.VAR_POSITIONAL
                for parameter in signature.parameters.values()
            )
        except (TypeError, ValueError):
            positional, has_varargs = [], True
        if has_varargs or len(positional) >= 3:
            return self.submit(definition, dry_run, context)
        return self.submit(definition, dry_run)


class HeadlessN8nAdapter:
    """Headless execution adapter; user-facing artifacts never contain n8n details."""

    def __init__(self, executor: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
        self.executor = executor
        self.published: dict[str, Mapping[str, Any]] = {}

    @property
    def backend_id(self) -> str:
        return "n8n"

    def status(self) -> Mapping[str, Any]:
        """Return a secret-free configuration receipt for Host diagnostics."""

        if self.executor is not None:
            describe = getattr(self.executor, "status", None)
            if callable(describe):
                try:
                    receipt = dict(describe())
                except Exception as exc:
                    return {
                        "backend": self.backend_id,
                        "configured": True,
                        "activation_ready": False,
                        "healthy": False,
                        "mode": "headless_execution_only",
                        "user_message": "外部工作流執行層無法連線；自動化不會假裝已啟用。",
                        "reason": f"health check failed: {type(exc).__name__}",
                    }
                return {
                    "backend": self.backend_id,
                    "mode": "headless_execution_only",
                    **receipt,
                }
        return {
            "backend": self.backend_id,
            "configured": self.executor is not None,
            "activation_ready": False,
            "healthy": False,
            "mode": "headless_execution_only",
            "user_message": (
                "外部工作流執行層已設定，但尚未取得 health check 證據。"
                if self.executor is not None
                else "外部工作流執行層尚未設定；自動化不會假裝已啟用。"
            ),
        }

    def dry_run(self, workflow: CompiledWorkflow) -> BackendResult:
        self._require(workflow)
        if self.executor is None:
            return BackendResult(
                self.backend_id,
                "dry_run",
                False,
                None,
                {
                    "submission_status": "pending",
                    "durable_submission_required": True,
                    "reason": "n8n executor is not configured",
                },
            )
        request = self._request(workflow)
        validate = getattr(self.executor, "dry_run", None)
        if callable(validate):
            try:
                detail = dict(validate(request))
            except Exception as exc:
                return BackendResult(
                    self.backend_id,
                    "dry_run",
                    False,
                    None,
                    {
                        "submission_status": "pending",
                        "durable_submission_required": True,
                        "recoverable": True,
                        "reason": f"n8n dry run failed: {type(exc).__name__}",
                    },
                )
            accepted = bool(detail.get("accepted", detail.get("validated", False)))
            return BackendResult(self.backend_id, "dry_run", accepted, None, detail)
        return BackendResult(
            self.backend_id,
            "dry_run",
            True,
            None,
            {
                "validated": True,
                "health_checked": False,
                "credential_refs_resolved": len(workflow.credential_refs),
            },
        )

    def activate(
        self,
        workflow: CompiledWorkflow,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> BackendResult:
        self._require(workflow)
        if self.executor is None:
            return BackendResult(
                self.backend_id,
                "activate",
                False,
                None,
                {
                    "submission_status": "pending",
                    "durable_submission_required": True,
                    "reason": "n8n executor is not configured",
                },
            )
        request = self._request(workflow, context=context)
        try:
            detail = dict(self.executor(request))
        except Exception as exc:
            # Gateway reachability must not turn an approved automation into a
            # fake active workflow or discard the durable submission.  The
            # controller will retain it as pending and retry it after restart.
            return BackendResult(
                self.backend_id,
                "activate",
                False,
                None,
                {
                    "submission_status": "pending",
                    "durable_submission_required": True,
                    "recoverable": True,
                    "reason": f"n8n API deployment failed: {type(exc).__name__}",
                },
            )
        accepted = bool(detail.get("accepted", detail.get("published", detail.get("workflow_id") is not None)))
        reference_value = detail.get("workflow_id") or detail.get("backend_reference")
        if accepted and not reference_value:
            accepted = False
            detail = {**detail, "reason": "n8n accepted submission without a durable reference"}
        reference = str(reference_value) if accepted and reference_value else None
        if accepted and reference:
            self.published[reference] = request
        return BackendResult(self.backend_id, "activate", accepted, reference, detail)

    def pause(self, workflow_id: str) -> BackendResult:
        return self._lifecycle("unpublish", workflow_id, "pause")

    def resume(self, workflow_id: str) -> BackendResult:
        return self._lifecycle("publish", workflow_id, "resume")

    def archive(self, workflow_id: str) -> BackendResult:
        return self._lifecycle("archive", workflow_id, "archive")

    def _lifecycle(self, executor_method: str, workflow_id: str, mode: str) -> BackendResult:
        if self.executor is None:
            return BackendResult(
                self.backend_id,
                mode,
                False,
                workflow_id or None,
                {"reason": "n8n executor is not configured"},
            )
        operation = getattr(self.executor, executor_method, None)
        if not callable(operation):
            return BackendResult(
                self.backend_id,
                mode,
                False,
                workflow_id or None,
                {"reason": f"n8n executor does not support {executor_method}"},
            )
        try:
            detail = dict(operation(workflow_id))
        except Exception as exc:
            return BackendResult(
                self.backend_id,
                mode,
                False,
                workflow_id or None,
                {"reason": f"n8n lifecycle request failed: {type(exc).__name__}"},
            )
        return BackendResult(
            self.backend_id,
            mode,
            bool(detail.get("accepted")),
            workflow_id or None,
            detail,
        )

    @staticmethod
    def _request(
        workflow: CompiledWorkflow,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime_context = {
            key: value
            for key, value in dict(context or {}).items()
            if key
            in {
                "automation_id",
                "automation_version",
                "submission_id",
                "idempotency_key",
                "target_backend_reference",
            }
        }
        return {
            "compiler_contract": {
                "schema_version": workflow.schema_version,
                "compiler_seal": workflow.compiler_seal,
                "backend": workflow.backend,
                "digest": workflow.digest,
            },
            "compiled_definition": dict(workflow.definition),
            "credential_refs": list(workflow.credential_refs),
            "idempotency_key": str(runtime_context.get("idempotency_key") or workflow.digest),
            "runtime_context": runtime_context,
        }

    def _require(self, workflow: CompiledWorkflow) -> None:
        WorkflowCompiler.require_compiler_product(workflow)
        if workflow.backend != self.backend_id:
            raise ValueError("workflow is not compiled for n8n")
        if not workflow.credential_refs:
            raise ValueError("headless n8n activation requires credential references")
