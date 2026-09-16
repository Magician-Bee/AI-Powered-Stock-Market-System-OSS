from ...brokers.declared_adapter import DeclaredBrokerAdapter


def build_adapter() -> DeclaredBrokerAdapter:
    return DeclaredBrokerAdapter(
        broker_id="fubon",
        official_url="https://www.fbs.com.tw/TradeAPI/docs/trading/prepare/",
        required_actions=[
            "open_account",
            "apply_certificate",
            "sign_api_agreement",
            "complete_api_test",
        ],
    )
