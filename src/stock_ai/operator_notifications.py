"""Host-owned operator notification sinks for local data operations.

The desktop sink is intentionally narrow: it only accepts a title and a
secret-minimized alert summary, invokes macOS Notification Center without a
shell, and returns a content-addressed provider receipt.  A zero exit status
means the operating system accepted the notification request; it does not
claim that a human acknowledged it.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


_APPLE_SCRIPT = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
""".strip()


class MacOSNotificationCenterSender:
    """Deliver one sanitized alert to the signed-in macOS desktop session."""

    sink_id = "macos_notification_center"

    def __init__(
        self,
        *,
        runner: CommandRunner = subprocess.run,
        system_name: Callable[[], str] = platform.system,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runner = runner
        self._system_name = system_name
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        alert = payload.get("alert") if isinstance(payload.get("alert"), Mapping) else payload
        source = _clean(alert.get("source_id") or "system", limit=80)
        dataset = _clean(alert.get("dataset") or "runtime", limit=80)
        code = _clean(alert.get("code") or "operator_alert", limit=80)
        severity = _clean(alert.get("severity") or "warning", limit=24).upper()
        title = _clean(f"Stock AI 資料告警 · {severity}", limit=120)
        body = _clean(f"{source}/{dataset} · {code}", limit=240)
        attempted_at = self._clock().astimezone(timezone.utc).isoformat()
        message_sha256 = hashlib.sha256(
            json.dumps(
                {"title": title, "body": body},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        base = {
            "schema_version": "stock_ai.macos_notification_center_receipt.v1",
            "sink": self.sink_id,
            "attempted_at": attempted_at,
            "message_sha256": message_sha256,
            "acknowledgement": "os_request_accepted_only",
        }
        if self._system_name() != "Darwin":
            return {
                **base,
                "accepted": False,
                "error": "macos_notification_center_unavailable",
            }
        try:
            result = self._runner(
                ["/usr/bin/osascript", "-e", _APPLE_SCRIPT, title, body],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                **base,
                "accepted": False,
                "error": f"notification_center_request_failed:{type(exc).__name__}",
            }
        if result.returncode != 0:
            return {
                **base,
                "accepted": False,
                "error": "notification_center_rejected_request",
                "exit_code": int(result.returncode),
            }
        return {**base, "accepted": True, "exit_code": 0}


def configured_operator_notification_sender(
    *,
    configured_sinks: str | Sequence[str] | None = None,
) -> MacOSNotificationCenterSender | None:
    """Build the local sink only when the host explicitly enables it."""

    raw: str | Sequence[str] = (
        configured_sinks
        if configured_sinks is not None
        else os.getenv("STOCK_AI_OPERATOR_ALERT_SINKS", "")
    )
    if isinstance(raw, str):
        names = {part.strip().casefold() for part in raw.split(",") if part.strip()}
    else:
        names = {str(part).strip().casefold() for part in raw if str(part).strip()}
    return MacOSNotificationCenterSender() if "desktop" in names else None


def _clean(value: Any, *, limit: int) -> str:
    text = " ".join(str(value).replace("\x00", " ").split())
    return text[:limit] or "-"
