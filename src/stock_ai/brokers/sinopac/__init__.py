from ...brokers.declared_adapter import DeclaredBrokerAdapter


def build_adapter() -> DeclaredBrokerAdapter:
    return DeclaredBrokerAdapter(
        broker_id="sinopac",
        official_url="https://sinotrade.github.io/tutor/token/",
        required_actions=[
            "open_account",
            "create_api_key",
            "complete_api_test",
        ],
    )
