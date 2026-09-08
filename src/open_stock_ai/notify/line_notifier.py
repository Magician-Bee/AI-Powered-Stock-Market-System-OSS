from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LineNotifier:
    enabled: bool = False

    def send_preview(self, message: str) -> dict:
        return {
            "schema_version": "open_stock_ai.notification_preview.v1",
            "sent": False,
            "enabled": self.enabled,
            "channel": "line",
            "message": message,
            "delivery_boundary": "preview_only_no_remote_send",
        }
