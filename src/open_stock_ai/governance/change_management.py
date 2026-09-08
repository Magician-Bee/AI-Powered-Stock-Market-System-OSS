"""Immutable change-set and order version pinning for governed execution."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "open_stock_ai.change_management_receipt.v1"
_PIN_FIELDS = ("code_sha256", "model_sha256", "data_sha256", "risk_policy_sha256")


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.casefold())


@dataclass(frozen=True, slots=True)
class ChangeSet:
    change_id: str
    code_sha256: str
    model_sha256: str
    data_sha256: str
    risk_policy_sha256: str
    approved_by: str
    approved_at: str
    change_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "change_id": self.change_id,
            "code_sha256": self.code_sha256,
            "model_sha256": self.model_sha256,
            "data_sha256": self.data_sha256,
            "risk_policy_sha256": self.risk_policy_sha256,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "change_sha256": self.change_sha256}

    def verify(self) -> bool:
        return hmac.compare_digest(hashlib.sha256(_canonical(self.payload())).hexdigest(), self.change_sha256)


class ChangeManagementError(ValueError):
    """Raised when a version pin is missing, invalid or would be rewritten."""


class ChangeManagementRegistry:
    def __init__(self, store: Any | None = None) -> None:
        self.store = store
        self._changes: dict[str, ChangeSet] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        if store is not None:
            for payload in store.load_change_sets():
                change = ChangeSet(
                    change_id=str(payload["change_id"]),
                    code_sha256=str(payload["code_sha256"]),
                    model_sha256=str(payload["model_sha256"]),
                    data_sha256=str(payload["data_sha256"]),
                    risk_policy_sha256=str(payload["risk_policy_sha256"]),
                    approved_by=str(payload["approved_by"]),
                    approved_at=str(payload["approved_at"]),
                    change_sha256=str(payload["change_sha256"]),
                )
                if not change.verify():
                    raise ChangeManagementError("change_set_durable_record_invalid")
                self._changes[change.change_id] = change
            for binding in store.load_order_bindings():
                if not verify_order_version_binding(binding):
                    raise ChangeManagementError("order_version_binding_durable_record_invalid")
                self._orders[str(binding["order_id"])] = dict(binding)

    def pin_change(
        self,
        change_id: str,
        *,
        code_sha256: str,
        model_sha256: str,
        data_sha256: str,
        risk_policy_sha256: str,
        approved_by: str,
        approved_at: str,
    ) -> ChangeSet:
        change_id = str(change_id or "").strip()
        actor = str(approved_by or "").strip()
        values = {
            "code_sha256": str(code_sha256 or "").casefold(),
            "model_sha256": str(model_sha256 or "").casefold(),
            "data_sha256": str(data_sha256 or "").casefold(),
            "risk_policy_sha256": str(risk_policy_sha256 or "").casefold(),
        }
        if not change_id or any(not _is_sha256(value) for value in values.values()):
            raise ChangeManagementError("all_change_version_hashes_are_required")
        if not actor or actor.casefold() in {"agent", "codex", "model", "ai", "system"}:
            raise ChangeManagementError("human_approval_required_for_change_set")
        if not str(approved_at or "").strip():
            raise ChangeManagementError("change_set_approval_timestamp_required")
        payload = {"schema_version": SCHEMA_VERSION, "change_id": change_id, **values, "approved_by": actor, "approved_at": approved_at}
        change = ChangeSet(
            change_id=change_id,
            code_sha256=values["code_sha256"],
            model_sha256=values["model_sha256"],
            data_sha256=values["data_sha256"],
            risk_policy_sha256=values["risk_policy_sha256"],
            approved_by=actor,
            approved_at=approved_at,
            change_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
        )
        existing = self._changes.get(change_id)
        if existing is not None and existing != change:
            raise ChangeManagementError("change_set_immutable_conflict")
        if self.store is not None:
            try:
                self.store.save_change_set(change.as_dict())
            except ValueError as exc:
                raise ChangeManagementError(str(exc)) from exc
        self._changes[change_id] = change
        return change

    def bind_order(self, order_id: str, *, change_id: str, bound_at: str | None = None) -> dict[str, Any]:
        order_id = str(order_id or "").strip()
        change = self._changes.get(str(change_id or "").strip())
        if not order_id or change is None:
            raise ChangeManagementError("order_change_set_not_found")
        existing = self._orders.get(order_id)
        if existing is not None:
            if not verify_order_version_binding(existing):
                raise ChangeManagementError("order_version_binding_invalid")
            if (
                existing["change_id"] != change.change_id
                or existing["change_sha256"] != change.change_sha256
                or any(existing[field] != getattr(change, field) for field in _PIN_FIELDS)
            ):
                raise ChangeManagementError("order_version_binding_immutable_conflict")
            # A caller can safely retry after a crash or timeout.  The first
            # durable binding owns ``bound_at``; a retry must return exactly
            # that immutable receipt instead of creating a timestamp conflict.
            return dict(existing)
        payload = {
            "schema_version": "open_stock_ai.order_version_binding.v1",
            "order_id": order_id,
            "change_id": change.change_id,
            "change_sha256": change.change_sha256,
            "code_sha256": change.code_sha256,
            "model_sha256": change.model_sha256,
            "data_sha256": change.data_sha256,
            "risk_policy_sha256": change.risk_policy_sha256,
            "bound_at": str(bound_at or datetime.now(timezone.utc).isoformat()),
        }
        binding = {**payload, "binding_sha256": hashlib.sha256(_canonical(payload)).hexdigest()}
        if self.store is not None:
            try:
                self.store.save_order_binding(binding)
            except ValueError as exc:
                raise ChangeManagementError(str(exc)) from exc
        self._orders[order_id] = binding
        return dict(binding)


def verify_order_version_binding(binding: dict[str, Any]) -> bool:
    required = {
        "schema_version", "order_id", "change_id", "change_sha256", "code_sha256",
        "model_sha256", "data_sha256", "risk_policy_sha256", "bound_at", "binding_sha256",
    }
    if set(binding) != required or binding.get("schema_version") != "open_stock_ai.order_version_binding.v1":
        return False
    if any(not _is_sha256(str(binding.get(field) or "")) for field in (*_PIN_FIELDS, "change_sha256", "binding_sha256")):
        return False
    expected = hashlib.sha256(_canonical({key: binding[key] for key in required if key != "binding_sha256"})).hexdigest()
    return hmac.compare_digest(expected, str(binding.get("binding_sha256") or ""))
