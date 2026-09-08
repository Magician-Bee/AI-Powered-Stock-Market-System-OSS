"""Account-owner hand-off contract for the Taishin read-only integration.

This module deliberately describes the first safe integration milestone rather
than attempting a login.  The Taishin API onboarding process includes
account-owner agreements and verification, so the Host may only prepare a
read-only market-data probe after those steps are complete.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


TAISHIN_HOME_URL = "https://mlapi.tssco.com.tw/web_api/service/home"
TAISHIN_PYTHON_QUOTE_GUIDE_URL = (
    "https://mlapi.tssco.com.tw/web_api/service/document/python-quote"
)


class TaishinReadonlyOnboardingStep(BaseModel):
    """One explicitly bounded step visible in the local settings UI."""

    model_config = ConfigDict(extra="forbid")

    step_id: Literal[
        "account_owner_authorization",
        "official_sdk_provenance",
        "secure_reference_setup",
        "isolated_quote_probe",
        "receipt_review",
    ]
    title: str
    detail: str
    account_owner_required: bool
    official_url: str | None = None


class TaishinReadonlyOnboardingPlan(BaseModel):
    """Safe, serialisable plan; it never contains credentials or account data."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.taishin_readonly_onboarding.v1"] = (
        "stock_ai.taishin_readonly_onboarding.v1"
    )
    broker_id: Literal["taishin"] = "taishin"
    broker_name: str = "台新證券數位 API"
    integration_scope: Literal["read_only_market_data"] = "read_only_market_data"
    live_trading_enabled: Literal[False] = False
    order_submission_enabled: Literal[False] = False
    account_snapshot_enabled: Literal[False] = False
    secret_entry_allowed_in_stock_ai: Literal[False] = False
    preferred_sdk_language: Literal["python"] = "python"
    official_home_url: str = TAISHIN_HOME_URL
    official_quote_guide_url: str = TAISHIN_PYTHON_QUOTE_GUIDE_URL
    prerequisites: list[str] = Field(
        default_factory=lambda: [
            "帳戶本人於台新官方頁完成 API 申請、風險文件與官方驗證。",
            "官方 PY_TradeD Python 行情 wheel 僅保存在專案外；Stock AI 只記錄來源、版本與 SHA-256。",
            "任何秘密只可由帳戶本人放入 macOS Keychain；本機專案、Agent 與 Log 都不接收秘密值。",
        ]
    )
    steps: list[TaishinReadonlyOnboardingStep] = Field(
        default_factory=lambda: [
            TaishinReadonlyOnboardingStep(
                step_id="account_owner_authorization",
                title="本人完成台新官方申請與驗證",
                detail="在台新數位 API 官方頁完成線上簽署及官方要求的驗證。此步驟由帳戶本人執行，Stock AI 不會替你登入或提交。",
                account_owner_required=True,
                official_url=TAISHIN_HOME_URL,
            ),
            TaishinReadonlyOnboardingStep(
                step_id="official_sdk_provenance",
                title="下載官方 PY_TradeD Python 行情 SDK 並記錄來源",
                detail="SDK 保留在專案外。隔離 Worker 只載入官方 wheel 的 PY_Trade_package；Host 會驗證下載來源、版本與 SHA-256，沒有官方可驗證資訊時不安裝。",
                account_owner_required=True,
                official_url=TAISHIN_PYTHON_QUOTE_GUIDE_URL,
            ),
            TaishinReadonlyOnboardingStep(
                step_id="secure_reference_setup",
                title="由本人在 macOS Keychain 建立安全引用",
                detail="只保存 Keychain reference；不會把密碼、OTP、憑證內容或帳號交給 Stock AI、模型、Log 或 Git。",
                account_owner_required=True,
            ),
            TaishinReadonlyOnboardingStep(
                step_id="isolated_quote_probe",
                title="執行隔離的唯讀行情 probe",
                detail="Host 只會對一檔受限代號依序登入、訂閱、等待官方行情 callback、取消訂閱並斷線；回條只保留事件類型、公開欄位名稱與 SHA-256。逾時或清理失敗一律停止。第一階段不讀取帳務、不建立委託、更不會送出交易。",
                account_owner_required=False,
            ),
            TaishinReadonlyOnboardingStep(
                step_id="receipt_review",
                title="檢視不可竄改的驗證收據",
                detail="只有真實的 SDK、唯讀 Session 與行情收據完整通過後，才會把唯讀能力標為已驗證；交易能力仍維持關閉。",
                account_owner_required=False,
            ),
        ]
    )


def build_taishin_readonly_onboarding_plan() -> TaishinReadonlyOnboardingPlan:
    """Return the exact first-milestone contract for the Settings UI/API."""

    return TaishinReadonlyOnboardingPlan()
