from ...brokers.declared_adapter import DeclaredBrokerAdapter


def build_adapter() -> DeclaredBrokerAdapter:
    return DeclaredBrokerAdapter(
        broker_id="yuanta",
        official_url="https://www.yuanta.com.tw/file-repository/content/API/page/index.html",
        required_actions=[
            "open_account",
            "sign_api_agreement",
            "complete_api_test",
            "configure_fixed_ip",
            "contact_broker_representative",
        ],
    )
