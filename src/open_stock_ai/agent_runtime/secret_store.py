from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KeychainSecretStore:
    """macOS Keychain adapter; secret values never enter SQLite, YAML, or logs."""

    service: str = "com.stock-ai.agent-runtime"

    @property
    def available(self) -> bool:
        return platform.system() == "Darwin"

    def set(self, account: str, value: str) -> None:
        self._require_available()
        if not account.strip() or not value:
            raise ValueError("Keychain account and value are required")
        self._run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
                value,
            ]
        )

    def get(self, account: str) -> str | None:
        self._require_available()
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise RuntimeError("macOS Keychain lookup failed")
        return result.stdout.rstrip("\n")

    def delete(self, account: str) -> bool:
        self._require_available()
        result = subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-s",
                self.service,
                "-a",
                account,
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 44:
            return False
        if result.returncode != 0:
            raise RuntimeError("macOS Keychain delete failed")
        return True

    def describe(self) -> dict[str, object]:
        return {
            "backend": "macOS Keychain",
            "service": self.service,
            "available": self.available,
            "secret_values_returned_by_diagnostics": False,
        }

    def _run(self, command: list[str]) -> None:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("macOS Keychain write failed")

    def _require_available(self) -> None:
        if not self.available:
            raise RuntimeError("macOS Keychain is unavailable on this platform")
