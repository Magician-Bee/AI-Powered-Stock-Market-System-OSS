from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


@dataclass(frozen=True, slots=True)
class RepairResult:
    value: Any
    repaired: bool
    strategies: tuple[str, ...]


class DeterministicRepair:
    """Repair syntax and known shapes without inventing domain meaning."""

    def repair(
        self,
        fragment: Any,
        *,
        aliases: dict[str, str] | None = None,
        known_wrappers: tuple[str, ...] = ("result", "data", "payload"),
    ) -> RepairResult:
        strategies: list[str] = []
        value = fragment
        if isinstance(value, bytes):
            value = value.decode("utf-8-sig")
            strategies.append("decode_utf8")
        if isinstance(value, str):
            text = value.lstrip("\ufeff")
            if text != value:
                strategies.append("remove_bom")
            stripped = _THINK_RE.sub("", text).strip()
            if stripped != text.strip():
                strategies.append("remove_reasoning_wrapper")
            fenced = _FENCE_RE.match(stripped)
            if fenced:
                stripped = fenced.group(1).strip()
                strategies.append("remove_json_fence")
            without_trailing = _TRAILING_COMMA_RE.sub(r"\1", stripped)
            if without_trailing != stripped:
                stripped = without_trailing
                strategies.append("remove_trailing_comma")
            extracted = self._extract_single_json_value(stripped)
            if extracted != stripped:
                stripped = extracted
                strategies.append("extract_json_value")
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Unsafe or ambiguous JSON cannot be repaired: {exc.msg}") from exc
        if isinstance(value, dict):
            for wrapper in known_wrappers:
                if set(value) == {wrapper} and isinstance(value[wrapper], (dict, list)):
                    value = value[wrapper]
                    strategies.append(f"unwrap_{wrapper}")
                    break
            if aliases:
                collisions = {alias for alias, target in aliases.items() if alias in value and target in value}
                if collisions:
                    raise ValueError(f"Alias collision is ambiguous: {sorted(collisions)}")
                mapped: dict[str, Any] = {}
                changed = False
                for key, item in value.items():
                    target = aliases.get(key, key)
                    mapped[target] = item
                    changed = changed or target != key
                if changed:
                    value = mapped
                    strategies.append("map_safe_aliases")
        return RepairResult(value=value, repaired=bool(strategies), strategies=tuple(strategies))

    @staticmethod
    def _extract_single_json_value(text: str) -> str:
        decoder = json.JSONDecoder()
        starts = [index for index, char in enumerate(text) if char in "{["]
        if not starts:
            return text
        # Only parse the earliest possible root. Searching later offsets after
        # that root fails can accidentally promote a valid nested fragment to
        # the whole response and silently discard model meaning.
        start = starts[0]
        try:
            _, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            return text
        candidate = text[start : start + end]
        suffix = text[start + end :].strip()
        if suffix and any(char in suffix for char in "{["):
            return text
        if candidate:
            return candidate
        return text
