from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentSecretStore:
    """Store optional Agent provider credentials outside project files.

    macOS uses the signed-in user's Keychain. Other platforms may supply
    credentials through environment variables until a native credential-store
    adapter is installed.
    """

    service: str = "com.openstockai.agent-runtime"

    _ENV_NAMES = {
        "openai-compatible-api-key": "STOCK_AI_OPENAI_API_KEY",
        "external-agent-token": "STOCK_AI_EXTERNAL_AGENT_TOKEN",
    }

    def get(self, account: str) -> str:
        environment = os.getenv(self._ENV_NAMES.get(account, ""))
        if environment:
            return environment
        if platform.system() != "Darwin":
            return ""
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.rstrip("\r\n") if result.returncode == 0 else ""

    def configured(self, account: str) -> bool:
        return bool(self.get(account))

    def set(self, account: str, value: str) -> None:
        secret = str(value or "")
        if not secret:
            self.delete(account)
            return
        if platform.system() != "Darwin":
            raise RuntimeError(
                f"Secure credential persistence is unavailable on this platform; "
                f"set the environment variable "
                f"{self._ENV_NAMES.get(account, 'for this provider')} instead."
            )
        result = subprocess.run(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            input=f"{secret}\n",
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError("macOS Keychain refused the provider credential")

    def delete(self, account: str) -> None:
        if platform.system() != "Darwin":
            return
        subprocess.run(
            [
                "/usr/bin/security",
                "delete-generic-password",
                "-s",
                self.service,
                "-a",
                account,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )


agent_secret_store = AgentSecretStore()


__all__ = ["AgentSecretStore", "agent_secret_store"]
