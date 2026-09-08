from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import BrokerId


_SECURE_REFERENCE_PREFIXES = (
    "keychain://",
    "credential-manager://",
    "keyring://",
    "vault://",
)


class BrokerSdkArtifactReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_sdk_artifact_receipt.v1"] = (
        "stock_ai.broker_sdk_artifact_receipt.v1"
    )
    broker_id: BrokerId
    source_url: str
    sdk_version: str
    artifact_name: str
    artifact_bytes: int = Field(gt=0)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at: datetime
    account_owner_confirmed_official_download: bool
    official_checksum_verified: bool
    install_allowed: bool


class CertificateHealthReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_certificate_health.v1"] = (
        "stock_ai.broker_certificate_health.v1"
    )
    broker_id: BrokerId
    certificate_reference: str
    status: Literal["unknown", "healthy", "warning", "expired", "unavailable"]
    exists: bool | None = None
    readable_by_worker: bool | None = None
    permissions_restricted: bool | None = None
    expires_at: datetime | None = None
    observed_at: datetime
    install_or_login_allowed: bool = False
    reasons: list[str] = Field(default_factory=list)

    @field_validator("certificate_reference")
    @classmethod
    def require_secure_reference(cls, value: str) -> str:
        if not value.startswith(_SECURE_REFERENCE_PREFIXES):
            raise ValueError(
                "certificate health accepts only secure-store references"
            )
        return value


class BrokerSdkProvisioner:
    """Record SDK provenance without copying vendor binaries into the project."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).expanduser().resolve()

    def record_artifact(
        self,
        *,
        broker_id: BrokerId,
        artifact_path: str | Path,
        source_url: str,
        sdk_version: str,
        account_owner_confirmed_official_download: bool,
        official_sha256: str | None = None,
    ) -> BrokerSdkArtifactReceipt:
        path = Path(artifact_path).expanduser().resolve()
        if path.is_relative_to(self.project_root):
            raise ValueError("vendor SDK artifacts must remain outside the project")
        if not path.is_file():
            raise FileNotFoundError(path)
        if not source_url.startswith("https://"):
            raise ValueError("SDK provenance requires an HTTPS official source URL")
        hasher = sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if official_sha256 is not None and digest != official_sha256:
            raise ValueError("SDK artifact hash does not match the official checksum")
        checksum_verified = official_sha256 is not None
        return BrokerSdkArtifactReceipt(
            broker_id=broker_id,
            source_url=source_url,
            sdk_version=sdk_version,
            artifact_name=path.name,
            artifact_bytes=path.stat().st_size,
            artifact_sha256=digest,
            recorded_at=datetime.now(timezone.utc),
            account_owner_confirmed_official_download=(
                account_owner_confirmed_official_download
            ),
            official_checksum_verified=checksum_verified,
            install_allowed=(
                account_owner_confirmed_official_download
                and checksum_verified
            ),
        )

    def certificate_health_from_host_metadata(
        self,
        *,
        broker_id: BrokerId,
        certificate_reference: str,
        exists: bool | None,
        readable_by_worker: bool | None,
        permissions_restricted: bool | None,
        expires_at: datetime | None,
    ) -> CertificateHealthReport:
        now = datetime.now(timezone.utc)
        expiry = expires_at
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        reasons: list[str] = []
        status: Literal[
            "unknown", "healthy", "warning", "expired", "unavailable"
        ] = "unknown"
        if exists is False or readable_by_worker is False:
            status = "unavailable"
            reasons.append("certificate_unavailable_to_worker")
        elif expiry is not None and expiry <= now:
            status = "expired"
            reasons.append("certificate_expired")
        elif (
            exists is True
            and readable_by_worker is True
            and permissions_restricted is True
            and expiry is not None
        ):
            status = "healthy"
        elif any(
            value is not None
            for value in (exists, readable_by_worker, permissions_restricted, expiry)
        ):
            status = "warning"
            if permissions_restricted is not True:
                reasons.append("certificate_permissions_not_verified")
            if expiry is None:
                reasons.append("certificate_expiry_not_verified")
        return CertificateHealthReport(
            broker_id=broker_id,
            certificate_reference=certificate_reference,
            status=status,
            exists=exists,
            readable_by_worker=readable_by_worker,
            permissions_restricted=permissions_restricted,
            expires_at=expiry,
            observed_at=now,
            install_or_login_allowed=status == "healthy",
            reasons=reasons,
        )
