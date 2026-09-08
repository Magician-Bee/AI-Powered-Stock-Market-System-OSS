from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .capabilities import BrokerId


_SECRET_REFERENCE_PREFIXES = (
    "keychain://",
    "credential-manager://",
    "keyring://",
    "vault://",
)


class BrokerAuthProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_id: BrokerId
    account_alias: str = Field(min_length=1, max_length=100)
    auth_method: Literal["certificate", "api_key", "certificate_and_api_key", "token"]
    secret_reference: str
    certificate_reference: str | None = None
    api_permission_enabled: bool = False
    market_data_enabled: bool = False
    trading_enabled: bool = False
    expires_at: datetime | None = None

    @field_validator("secret_reference", "certificate_reference")
    @classmethod
    def only_allow_secure_references(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(_SECRET_REFERENCE_PREFIXES):
            raise ValueError(
                "broker secrets must be secure-store references, never plaintext or file content"
            )
        return value


class HumanAuthorizationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_authorization.v1"] = (
        "stock_ai.broker_authorization.v1"
    )
    broker_id: BrokerId
    state: Literal[
        "not_started",
        "requires_user_action",
        "ready_for_readonly_probe",
        "readonly_verified",
        "live_permission_recorded",
    ] = "not_started"
    required_actions: list[
        Literal[
            "open_account",
            "sign_api_agreement",
            "apply_certificate",
            "create_api_key",
            "complete_api_test",
            "configure_fixed_ip",
            "contact_broker_representative",
        ]
    ] = Field(default_factory=list)
    official_page_type: Literal[
        "api_onboarding",
        "certificate",
        "api_key",
        "testing",
    ] = "api_onboarding"
    official_url: str
    safe_to_continue_automatically: bool = False
    user_confirmed_at: datetime | None = None

    @field_validator("safe_to_continue_automatically")
    @classmethod
    def authorization_never_auto_continues(cls, value: bool) -> bool:
        if value:
            raise ValueError("broker authorization must pause for the account owner")
        return value
