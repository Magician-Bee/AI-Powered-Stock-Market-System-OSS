from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from ..model_router import (
    HostModelBudget,
    HostModelExecutor,
    ModelExecutionResult,
    ModelRole,
    ProviderNeutralRequest,
)
from .contracts import ErrorReceipt, ModelPatch
from .deterministic import DeterministicRepair


_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(\d+)\]")


class PatchValidationError(ValueError):
    pass


class ModelPatchValidator:
    """Validate a model-produced local patch before atomically applying it."""

    def apply(
        self,
        document: Any,
        patch: ModelPatch | dict[str, Any],
        *,
        receipt: ErrorReceipt,
        allowed_scopes: tuple[str, ...],
        expected_schema: dict[str, Any],
        max_operations: int = 8,
    ) -> Any:
        try:
            candidate_patch = ModelPatch.from_dict(patch) if isinstance(patch, dict) else patch
        except (TypeError, ValueError) as exc:
            raise PatchValidationError(str(exc)) from exc
        if candidate_patch.repair_for != receipt.error_id:
            raise PatchValidationError("Patch does not match the Error Receipt")
        if not allowed_scopes:
            raise PatchValidationError("At least one patch scope is required")
        if len(candidate_patch.patch) > max(1, int(max_operations)):
            raise PatchValidationError("Patch contains too many operations")
        operation_paths = [item.path for item in candidate_patch.patch]
        if len(operation_paths) != len(set(operation_paths)):
            raise PatchValidationError("Patch contains duplicate target paths")
        candidate = copy.deepcopy(document)
        for operation in candidate_patch.patch:
            if not self._is_allowed(operation.path, allowed_scopes):
                raise PatchValidationError(f"Patch path is outside allowed scope: {operation.path}")
            tokens = self._parse_path(operation.path)
            self._set(candidate, tokens, operation.value, add=operation.op == "add")
        errors = validate_json_schema(candidate, expected_schema)
        if errors:
            raise PatchValidationError("Patched document failed schema validation: " + "; ".join(errors))
        return candidate

    @staticmethod
    def _is_allowed(path: str, scopes: tuple[str, ...]) -> bool:
        return any(path == scope or path.startswith(scope + ".") or path.startswith(scope + "[") for scope in scopes)

    @staticmethod
    def _parse_path(path: str) -> list[str | int]:
        if not path.startswith("$"):
            raise PatchValidationError("Patch path must be a JSONPath beginning with $")
        tokens: list[str | int] = []
        cursor = 1
        for match in _PATH_TOKEN.finditer(path, 1):
            if match.start() != cursor:
                raise PatchValidationError(f"Unsupported JSONPath syntax: {path}")
            tokens.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
            cursor = match.end()
        if cursor != len(path) or not tokens:
            raise PatchValidationError(f"Unsupported JSONPath syntax: {path}")
        return tokens

    @staticmethod
    def _set(document: Any, tokens: list[str | int], value: Any, *, add: bool) -> None:
        parent = document
        for token in tokens[:-1]:
            try:
                parent = parent[token]
            except (KeyError, IndexError, TypeError) as exc:
                raise PatchValidationError("Patch parent path does not exist") from exc
        leaf = tokens[-1]
        if isinstance(parent, dict) and isinstance(leaf, str):
            if not add and leaf not in parent:
                raise PatchValidationError("replace requires an existing field")
            parent[leaf] = copy.deepcopy(value)
            return
        if isinstance(parent, list) and isinstance(leaf, int):
            if add and leaf == len(parent):
                parent.append(copy.deepcopy(value))
                return
            if leaf < 0 or leaf >= len(parent):
                raise PatchValidationError("Patch array index is out of range")
            parent[leaf] = copy.deepcopy(value)
            return
        raise PatchValidationError("Patch target type does not match its path")


@dataclass(frozen=True, slots=True)
class ModelRepairOutcome:
    value: Any
    strategy: str
    receipt: ErrorReceipt
    model_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    deterministic_strategies: tuple[str, ...] = ()


class HostModelRepairPipeline:
    """Run L0 deterministic repair, then a budgeted L1 model-local patch.

    The provider receives only the failed fragment, Error Receipt, complete
    expected schema and Host-selected patch scopes.  It never receives an
    execution capability.  The original document remains unchanged unless the
    returned patch is receipt-bound, scope-safe and schema-valid.
    """

    def __init__(
        self,
        executor: HostModelExecutor,
        *,
        deterministic: DeterministicRepair | None = None,
        validator: ModelPatchValidator | None = None,
    ) -> None:
        self.executor = executor
        self.deterministic = deterministic or DeterministicRepair()
        self.validator = validator or ModelPatchValidator()

    async def repair(
        self,
        document: Any,
        *,
        receipt: ErrorReceipt,
        allowed_scopes: tuple[str, ...],
        expected_schema: dict[str, Any],
        budget: HostModelBudget,
        aliases: dict[str, str] | None = None,
        reserve_output_tokens: int = 512,
    ) -> ModelRepairOutcome:
        self._validate_scopes(receipt, allowed_scopes)
        original = copy.deepcopy(document)
        fragment = self._get(original, self.validator._parse_path(receipt.location))

        try:
            repaired = self.deterministic.repair(fragment, aliases=aliases)
        except (TypeError, UnicodeError, ValueError):
            repaired = None
        if repaired is not None and repaired.repaired:
            try:
                candidate = self.validator.apply(
                    original,
                    {
                        "repair_for": receipt.error_id,
                        "patch": [
                            {
                                "op": "replace",
                                "path": receipt.location,
                                "value": repaired.value,
                            }
                        ],
                    },
                    receipt=receipt,
                    allowed_scopes=allowed_scopes,
                    expected_schema=expected_schema,
                )
            except PatchValidationError:
                pass
            else:
                return ModelRepairOutcome(
                    value=candidate,
                    strategy="host_repair",
                    receipt=receipt,
                    deterministic_strategies=repaired.strategies,
                )

        request = ProviderNeutralRequest(
            task="Repair only the allowed JSON scope. Return a JSON patch; do not answer the user or call tools.",
            context={
                "error_receipt": receipt.to_dict(),
                "failed_fragment": fragment,
                "allowed_patch_scopes": list(allowed_scopes),
            },
            tool_schemas=(),
            evidence=(),
            plan={},
            memory=(),
            # Keep both schemas on the non-compressible output-contract side
            # of ProviderNeutralRequest. A small-model context trim must never
            # remove fields from either the patch or target document contract.
            output_contract={
                **self.patch_output_contract(),
                "x-target-document-schema": copy.deepcopy(expected_schema),
            },
        )
        execution = await self.executor.execute(
            ModelRole.REPAIR,
            request,
            budget=budget,
            reserve_output_tokens=reserve_output_tokens,
        )
        patch = self._extract_patch(execution)
        candidate = self.validator.apply(
            original,
            patch,
            receipt=receipt,
            allowed_scopes=allowed_scopes,
            expected_schema=expected_schema,
        )
        return ModelRepairOutcome(
            value=candidate,
            strategy="model_local_patch",
            receipt=receipt,
            model_id=execution.route.profile.model_id,
            input_tokens=execution.input_tokens,
            output_tokens=execution.output_tokens,
        )

    @staticmethod
    def patch_output_contract() -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["repair_for", "patch"],
            "additionalProperties": False,
            "properties": {
                "repair_for": {"type": "string"},
                "patch": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["op", "path", "value"],
                        "additionalProperties": False,
                        "properties": {
                            "op": {"type": "string", "enum": ["add", "replace"]},
                            "path": {"type": "string"},
                            "value": {},
                        },
                    },
                },
            },
        }

    @staticmethod
    def _validate_scopes(receipt: ErrorReceipt, scopes: tuple[str, ...]) -> None:
        if not scopes:
            raise PatchValidationError("At least one patch scope is required")
        if any(
            not (
                scope == receipt.location
                or scope.startswith(receipt.location + ".")
                or scope.startswith(receipt.location + "[")
            )
            for scope in scopes
        ):
            raise PatchValidationError(
                "Allowed patch scope must be inside the Error Receipt location"
            )

    @staticmethod
    def _get(document: Any, tokens: list[str | int]) -> Any:
        value = document
        for token in tokens:
            try:
                value = value[token]
            except (KeyError, IndexError, TypeError) as exc:
                raise PatchValidationError("Error Receipt location does not exist") from exc
        return copy.deepcopy(value)

    def _extract_patch(self, execution: ModelExecutionResult) -> ModelPatch | dict[str, Any]:
        value = execution.output
        if isinstance(value, ModelPatch):
            return value
        if isinstance(value, str):
            try:
                value = self.deterministic.repair(value).value
            except (TypeError, UnicodeError, ValueError) as exc:
                raise PatchValidationError("Repair model did not return valid JSON") from exc
        if isinstance(value, dict):
            if "repair_for" in value and "patch" in value:
                return value
            for key in ("structured_result", "model_patch", "result"):
                nested = value.get(key)
                if isinstance(nested, dict) and "repair_for" in nested and "patch" in nested:
                    return nested
        raise PatchValidationError("Repair model response did not contain a patch")


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Small deterministic validator for repair gates; supports runtime schemas used here."""
    errors: list[str] = []
    expected_type = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if expected_type in type_map and (isinstance(value, bool) and expected_type in {"number", "integer"} or not isinstance(value, type_map[expected_type])):
        return [f"{path}: expected {expected_type}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")
    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required field is missing")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: additional field is not allowed")
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                errors.extend(validate_json_schema(value[key], child_schema, f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(validate_json_schema(item, schema["items"], f"{path}[{index}]"))
    return errors
