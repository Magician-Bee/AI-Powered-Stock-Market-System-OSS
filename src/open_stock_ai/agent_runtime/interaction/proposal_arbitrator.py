from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class UserInputKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    HYPOTHESIS = "hypothesis"
    CONSTRAINT = "constraint"
    CORRECTION = "correction"
    PROPOSAL = "proposal"
    COMMAND = "command"
    QUESTION = "question"
    APPROVAL = "approval"
    REJECTION = "rejection"


class ArbitrationDecision(StrEnum):
    ACCEPT = "accept"
    MODIFY = "modify"
    REJECT = "reject"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class ArbitrationResult:
    input_kind: UserInputKind
    decision: ArbitrationDecision
    user_input: str
    evidence_compatible: bool | None
    risk_level: str
    impact_level: str
    recommendation: str
    rationale: tuple[str, ...]
    requires_user_decision: bool = False


class ProposalArbitrator:
    _PROHIBITED_TRADE_TOKENS = (
        "不要風控", "略過風控", "繞過風控", "直接下單",
        "不用批准", "真實資金", "bypass risk", "without approval",
        "live funds", "live money",
    )
    _APPROVAL_BYPASS_PATTERN = re.compile(
        r"(?:不需要|不用|無需|毋須|免|不經).{0,12}(?:批准|核准|確認|同意)"
    )
    _LIVE_ACCOUNT_MARKERS = (
        "真實帳戶",
        "實盤",
        "真實資金",
        "真實交易",
        "live account",
        "live trade",
        "live order",
        "real account",
        "real trade",
        "real order",
        "broker account",
        "券商帳戶",
        "證券帳戶",
        "台新證券",
    )
    _TRADE_ACTION_MARKERS = (
        "買進",
        "買入",
        "賣出",
        "下單",
        "送單",
        "成交",
        "buy",
        "sell",
        "place order",
        "submit order",
    )
    _LOCAL_PAPER_MARKERS = (
        "紙上",
        "模擬交易",
        "模擬下單",
        "paper trade",
        "paper order",
        "paper_execute",
        "sandbox order",
    )
    _NEGATED_LIVE_MARKER = re.compile(
        r"(?:不是|並非|非|不做|不要|不得|不進行|不建立|不使用|不送往|禁止|勿|do not|not a)"
        r"(?:.{0,16})?(?:本次|任何)?\s*$",
        re.IGNORECASE,
    )

    def classify(self, message: str) -> UserInputKind:
        value = message.strip().casefold()
        if value.endswith(("?", "？")) or value.startswith(("為什麼", "怎麼", "是否")):
            return UserInputKind.QUESTION
        # An explicit preference often contains a negative clause such as
        # "n8n 不是第二大腦".  It remains a preference, not a correction of a
        # previously supplied fact; classify it first so it can enter governed
        # advisory memory.
        if any(token in value for token in ("我偏好", "我希望", "我喜歡")):
            return UserInputKind.PREFERENCE
        if any(token in value for token in ("不是", "更正", "應為", "我說錯")):
            return UserInputKind.CORRECTION
        if any(token in value for token in ("同意", "批准", "確認執行", "approve")):
            return UserInputKind.APPROVAL
        if any(token in value for token in ("拒絕", "不同意", "不要執行", "reject")):
            return UserInputKind.REJECTION
        if any(token in value for token in ("必須", "不可", "不能超過", "限制")):
            return UserInputKind.CONSTRAINT
        if any(token in value for token in ("我猜", "可能是", "假設")):
            return UserInputKind.HYPOTHESIS
        if any(
            token in value
            for token in (
                "建議", "改成", "只看", "移除", "新增",
                "不要風控", "略過風控", "繞過風控", "直接下單",
            )
        ):
            return UserInputKind.PROPOSAL
        if re.match(r"^(請|幫我|停止|取消|開始|執行)", value):
            return UserInputKind.COMMAND
        return UserInputKind.FACT

    def is_prohibited_trade_proposal(self, message: str) -> bool:
        """Return whether a request crosses a non-overridable live-trade boundary.

        The Agent has no live-order authority.  A request to act through a
        named real account must be rejected before the provider can turn it
        into analysis, paper-preview, approval or Automation work.  This is
        deliberately narrower than a generic broker/news question: it needs
        both a live-account marker and an order action.
        """

        value = message.strip().casefold()
        # A bounded local paper order is explicitly non-production.  Do not
        # reject it merely because the user also says "不是實盤交易" or asks
        # for a paper-fill receipt: those phrases contain the same words as a
        # live-order request but state the opposite.  This exception applies
        # only when every live-account marker is negated; a named/real account
        # request remains prohibited even if it also mentions a simulation.
        if self._is_explicit_local_paper_request(value):
            return False
        if any(token in value for token in self._PROHIBITED_TRADE_TOKENS):
            return True
        if self._APPROVAL_BYPASS_PATTERN.search(value):
            return True
        return (
            any(token in value for token in self._LIVE_ACCOUNT_MARKERS)
            and self._requests_trade_action(value)
        )

    @classmethod
    def _is_explicit_local_paper_request(cls, value: str) -> bool:
        if not any(marker in value for marker in cls._LOCAL_PAPER_MARKERS):
            return False
        if not cls._requests_trade_action(value):
            return False
        for marker in cls._LIVE_ACCOUNT_MARKERS:
            start = 0
            while True:
                index = value.find(marker, start)
                if index < 0:
                    break
                prefix = value[max(0, index - 20):index]
                if not cls._NEGATED_LIVE_MARKER.search(prefix):
                    return False
                start = index + len(marker)
        return True

    @classmethod
    def _requests_trade_action(cls, value: str) -> bool:
        """Return only affirmative trade actions.

        A live-account marker by itself is not prohibited: research and
        artifact requests routinely state constraints such as ``不下單`` or
        ``不得送出真實交易``.  Treating the action substring in those clauses
        as affirmative made the Host reject safe work before the provider was
        even allowed to create a local artifact.  Inspect each occurrence so
        a separate affirmative action in the same message still remains
        detectable.
        """

        negation_prefix = re.compile(
            r"(?:不要|不需|不必|不可|不能|不會|不得|不再|不|別|勿|禁止|避免|"
            r"do not|don't|never)(?:\\s|[，、；;。,.]){0,2}(?:要|會|可|得|進行|建立|送出)?(?:\\s){0,2}$",
            re.IGNORECASE,
        )
        for action in cls._TRADE_ACTION_MARKERS:
            start = 0
            while True:
                index = value.find(action, start)
                if index < 0:
                    break
                prefix = value[max(0, index - 16):index]
                if not negation_prefix.search(prefix):
                    return True
                start = index + len(action)
        return False

    def arbitrate(
        self,
        message: str,
        *,
        evidence_compatible: bool | None,
        risk_level: str = "low",
        impact_level: str = "local",
        safe_alternative: str | None = None,
        rationale: tuple[str, ...] = (),
        input_kind: UserInputKind | None = None,
    ) -> ArbitrationResult:
        kind = input_kind or self.classify(message)
        risk = risk_level.casefold()
        impact = impact_level.casefold()
        reasons = list(rationale)
        if risk in {"critical", "prohibited"}:
            decision = ArbitrationDecision.REJECT
            recommendation = safe_alternative or "保留安全邊界並拒絕此方案。"
            reasons.append("The proposal conflicts with a non-overridable safety boundary.")
        elif risk == "high":
            decision = ArbitrationDecision.MODIFY if safe_alternative else ArbitrationDecision.ASK
            recommendation = safe_alternative or "先釐清風險承擔方式，再決定是否採用。"
            reasons.append("High-risk proposals require a safer alternative or explicit decision.")
        elif evidence_compatible is False:
            decision = ArbitrationDecision.MODIFY if safe_alternative else ArbitrationDecision.REJECT
            recommendation = safe_alternative or "維持目前有證據支持的方案。"
            reasons.append("Available evidence does not support the proposal as stated.")
        elif evidence_compatible is None or impact in {"global", "irreversible"}:
            decision = ArbitrationDecision.ASK
            recommendation = safe_alternative or "先補足證據或影響分析，再共同決定。"
            reasons.append("The proposal has unresolved evidence or global impact.")
        else:
            decision = ArbitrationDecision.ACCEPT
            recommendation = safe_alternative or "採用此方案，並保留後續驗證。"
            reasons.append("The proposal is evidence-compatible and locally reversible.")
        return ArbitrationResult(
            input_kind=kind,
            decision=decision,
            user_input=message,
            evidence_compatible=evidence_compatible,
            risk_level=risk,
            impact_level=impact,
            recommendation=recommendation,
            rationale=tuple(dict.fromkeys(reasons)),
            requires_user_decision=decision == ArbitrationDecision.ASK,
        )
