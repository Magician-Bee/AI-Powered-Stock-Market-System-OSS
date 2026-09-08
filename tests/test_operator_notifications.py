from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from stock_ai.operator_notifications import (
    MacOSNotificationCenterSender,
    configured_operator_notification_sender,
)


def test_macos_notification_sender_uses_argument_vector_and_secret_minimized_copy() -> None:
    calls: list[tuple[list[str], dict]] = []

    def runner(command: list[str], **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    sender = MacOSNotificationCenterSender(
        runner=runner,
        system_name=lambda: "Darwin",
        clock=lambda: datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc),
    )
    receipt = sender(
        {
            "alert": {
                "source_id": "twse_openapi",
                "dataset": "prices_daily",
                "severity": "error",
                "code": "missing_partition",
                "details": {"api_token": "must-never-be-rendered"},
            }
        }
    )

    assert receipt["accepted"] is True
    assert receipt["sink"] == "macos_notification_center"
    assert len(receipt["message_sha256"]) == 64
    command, options = calls[0]
    assert command[0] == "/usr/bin/osascript"
    assert command[-2] == "Stock AI 資料告警 · ERROR"
    assert command[-1] == "twse_openapi/prices_daily · missing_partition"
    assert "must-never-be-rendered" not in " ".join(command)
    assert options == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 5,
    }


def test_macos_notification_sender_fails_closed_off_macos_without_invocation() -> None:
    called = False

    def runner(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("runner must not be called")

    receipt = MacOSNotificationCenterSender(
        runner=runner,
        system_name=lambda: "Linux",
    )({"code": "database_pressure", "severity": "warning"})

    assert called is False
    assert receipt["accepted"] is False
    assert receipt["error"] == "macos_notification_center_unavailable"


def test_operator_sender_requires_explicit_host_configuration() -> None:
    assert configured_operator_notification_sender(configured_sinks="") is None
    assert configured_operator_notification_sender(configured_sinks="telegram") is None
    assert isinstance(
        configured_operator_notification_sender(configured_sinks="desktop"),
        MacOSNotificationCenterSender,
    )
