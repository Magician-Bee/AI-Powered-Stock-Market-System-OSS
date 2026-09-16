from __future__ import annotations


class BrokerError(RuntimeError):
    """Base error for the host-controlled broker integration boundary."""


class BrokerCapabilityUnavailable(BrokerError):
    pass


class BrokerAuthorizationRequired(BrokerError):
    pass


class BrokerSecretPolicyError(BrokerError):
    pass


class BrokerLiveTradingDisabled(BrokerError):
    pass


class BrokerReconciliationRequired(BrokerError):
    pass
