from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .contracts import BrokerSecretPolicyError


SecretBackend = Literal["keychain", "credential-manager", "keyring", "vault"]


@dataclass(frozen=True)
class SecretReference:
    backend: SecretBackend
    locator: str

    def uri(self) -> str:
        if not self.locator or "://" in self.locator:
            raise BrokerSecretPolicyError("invalid secret locator")
        return f"{self.backend}://{self.locator}"


class BrokerSecretStore(Protocol):
    def put_reference(self, broker_id: str, name: str, reference: SecretReference) -> str: ...

    def get_reference(self, broker_id: str, name: str) -> str | None: ...


class InMemorySecretReferenceStore:
    """Stores only secure-store URIs. It never accepts or returns secret values."""

    def __init__(self) -> None:
        self._references: dict[tuple[str, str], str] = {}

    def put_reference(self, broker_id: str, name: str, reference: SecretReference) -> str:
        uri = reference.uri()
        self._references[(broker_id, name)] = uri
        return uri

    def get_reference(self, broker_id: str, name: str) -> str | None:
        return self._references.get((broker_id, name))
