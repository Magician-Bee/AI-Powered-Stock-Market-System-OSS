from ...brokers.declared_adapter import DeclaredBrokerAdapter


def build_adapter() -> DeclaredBrokerAdapter:
    return DeclaredBrokerAdapter(
        broker_id="masterlink",
        official_url=(
            "https://ml-fugle-api.masterlink.com.tw/FugleSDK/docs/trading/quickstart/"
        ),
        required_actions=[
            "open_account",
            "sign_api_agreement",
            "apply_certificate",
            "complete_api_test",
        ],
    )
