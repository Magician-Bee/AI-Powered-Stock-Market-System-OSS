from stock_ai.financial_anomalies import detect_financial_anomalies


def test_financial_anomaly_detector_covers_required_categories():
    flags = detect_financial_anomalies(
        income={"revenue": 110, "gross_profit": 33, "net_income": 20},
        previous_income={"revenue": 100, "gross_profit": 45, "net_income": 18},
        balance={"accounts_receivable": 40, "inventory": 50, "total_assets": 200},
        previous_balance={"accounts_receivable": 20, "inventory": 20, "total_assets": 200},
        cash_flow={"operating_cash_flow": 5},
    )
    by_code = {item["code"]: item for item in flags}
    assert set(by_code) == {"receivables", "inventory", "cash_conversion", "gross_margin", "one_time_gain_loss"}
    assert by_code["receivables"]["triggered"] is True
    assert by_code["inventory"]["triggered"] is True
    assert by_code["cash_conversion"]["triggered"] is True
    assert by_code["gross_margin"]["triggered"] is True
    assert by_code["one_time_gain_loss"]["status"] == "unavailable"


def test_one_time_gain_is_flagged_only_when_official_field_exists():
    flags = detect_financial_anomalies(
        income={"revenue": 100, "gross_profit": 40, "net_income": 10, "one_time_gain_loss": 4},
        previous_income={"revenue": 100, "gross_profit": 40, "net_income": 10},
        balance={"accounts_receivable": 10, "inventory": 10, "total_assets": 100},
        previous_balance={"accounts_receivable": 10, "inventory": 10, "total_assets": 100},
        cash_flow={"operating_cash_flow": 10},
    )
    item = next(x for x in flags if x["code"] == "one_time_gain_loss")
    assert item["triggered"] is True
