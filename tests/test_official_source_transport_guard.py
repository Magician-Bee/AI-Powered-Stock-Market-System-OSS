from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from open_stock_ai.agent_runtime import AgentRunContext

import stock_ai.guidance_tracking as guidance
import stock_ai.industry_metrics as industry
import stock_ai.phase1_data as phase1
import stock_ai.taiwan_official as official
import stock_ai.twse_openapi as twse
import stock_ai.basic_valuation as valuation
import stock_ai.tdcc_holding_history as tdcc
import stock_ai.liquidity as liquidity
import stock_ai.valuation_percentiles as percentiles
import stock_ai.monthly_revenue as monthly_revenue
import stock_ai.income_statement as income_statement
import stock_ai.cash_flow_statement as cash_flow
import stock_ai.balance_sheet as balance_sheet
import stock_ai.mvp_features as notifications
import stock_ai.realtime_quotes as realtime
import stock_ai.intraday_candles as candles
import stock_ai.agent_general_tools as general_tools
import stock_ai.short_daytrade_history as short_daytrade
import stock_ai.n8n_automation as n8n
import stock_ai.services as services


ROOT = Path(__file__).resolve().parents[1]
STOCK_AI = ROOT / "src" / "stock_ai"


class _Response:
    status = 200

    def __init__(self, payload):
        self.headers = {}
        self._payload = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


class _Guard:
    def __init__(self):
        self.scopes: list[str] = []

    def call_sync(self, scope, operation):
        self.scopes.append(scope)
        return operation()

    async def call(self, scope, operation):
        self.scopes.append(scope)
        return await operation()


class _AsyncClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args, **_kwargs):
        return self.response


def test_every_stock_ai_network_client_module_declares_the_shared_transport_guard():
    """Keep future provider/source HTTP or WebSocket clients fail-closed.

    The load-test runner lives in a different package on purpose: it measures
    a caller-supplied endpoint at a configured rate and must not be throttled
    by the application provider/source governor.  This audit is specifically
    for production Stock AI network clients.
    """

    guarded_modules: list[Path] = []
    unguarded_modules: list[Path] = []
    for path in sorted(STOCK_AI.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports_network_client = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports_network_client = imports_network_client or any(
                    alias.name.split(".", 1)[0]
                    in {"httpx", "requests", "aiohttp", "websockets"}
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
                imports_network_client = imports_network_client or any(
                    alias.name in {"urlopen", "build_opener"} for alias in node.names
                )
        if not imports_network_client:
            continue
        source = path.read_text(encoding="utf-8")
        if (
            "default_external_transport_guard" in source
            or "ExternalTransportGuard" in source
            or "default_broker_transport_guard" in source
            or "BrokerTransportGuard" in source
        ):
            guarded_modules.append(path.relative_to(ROOT))
        else:
            unguarded_modules.append(path.relative_to(ROOT))

    assert unguarded_modules == []
    assert guarded_modules


def test_industry_official_fetch_uses_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(industry, "default_external_transport_guard", lambda: guard)
    monkeypatch.setattr(industry, "urlopen", lambda *_args, **_kwargs: _Response([{"欄位": "值"}]))

    assert industry._default_fetch_json(industry.TWSE_FINANCIAL_HOLDING_INCOME_URL)
    assert guard.scopes == [
        f"source:twse_openapi:industry_metrics:{industry.TWSE_FINANCIAL_HOLDING_INCOME_URL}"
    ]


def test_guidance_official_fetch_uses_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(guidance, "default_external_transport_guard", lambda: guard)
    monkeypatch.setattr(guidance, "urlopen", lambda *_args, **_kwargs: _Response([]))

    assert guidance._fetch_json(guidance.TWSE_FORECAST_ACHIEVEMENT_URL) == []
    assert guard.scopes == ["source:twse_openapi:financial_guidance"]


def test_dynamic_twse_fetch_uses_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(twse, "default_external_transport_guard", lambda: guard)
    monkeypatch.setattr(twse, "urlopen", lambda *_args, **_kwargs: _Response([{ "code": "2330" }]))
    twse.fetch_twse_openapi_path.cache_clear()

    result = twse.fetch_twse_openapi_path("/exchangeReport/STOCK_DAY_ALL", limit=1)

    assert result["data"] == [{"code": "2330"}]
    assert guard.scopes == ["source:twse_openapi:dynamic:/exchangeReport/STOCK_DAY_ALL"]


def test_taiwan_official_loader_uses_dataset_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(official, "default_external_transport_guard", lambda: guard)
    monkeypatch.setattr(official, "urlopen", lambda *_args, **_kwargs: _Response([]))

    assert official._get_json(official.TWSE_COMPANIES) == []
    assert guard.scopes == ["source:twse_openapi:twse_companies"]


def test_phase1_loader_uses_dataset_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(phase1, "default_external_transport_guard", lambda: guard)
    monkeypatch.setattr(phase1, "urlopen", lambda *_args, **_kwargs: _Response([]))

    assert phase1._get_json(phase1.TWSE_T86_URL.format(date="20260826")) == []
    assert guard.scopes == ["source:twse_official_web:twse_institutional_flow"]


def test_basic_valuation_loader_uses_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(valuation, "default_external_transport_guard", lambda: guard)
    response = type("Response", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: [],
    })()
    monkeypatch.setattr(valuation.httpx, "get", lambda *_args, **_kwargs: response)
    valuation._OFFICIAL_DAILY_VALUATION_ROWS.clear()

    assert valuation._official_daily_valuation_rows("twse_daily_valuation") == []
    assert guard.scopes == ["source:twse_openapi:twse_daily_valuation"]


def test_tdcc_loader_uses_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(tdcc, "default_external_transport_guard", lambda: guard)
    response = type("Response", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: [{
            "資料日期": "20260826",
            "證券代號": "2330",
            "持股分級": "17",
            "人數": "100",
            "股數": "1000",
            "占集保庫存數比例%": "100",
        }],
    })()
    monkeypatch.setattr(tdcc.httpx, "get", lambda *_args, **_kwargs: response)

    assert len(tdcc.fetch_tdcc_holding_distribution("2330.TW")["distribution"]) == 1
    assert guard.scopes == ["source:tdcc_openapi:holding_distribution"]


def test_liquidity_share_revision_uses_transport_guard(monkeypatch, tmp_path):
    guard = _Guard()
    monkeypatch.setattr(liquidity, "default_external_transport_guard", lambda: guard)
    response = type("Response", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: [{
            "公司代號": "2330",
            "已發行普通股數或TDR原股發行股數": "1000",
            "出表日期": "1150826",
        }],
    })()
    monkeypatch.setattr(liquidity.httpx, "get", lambda *_args, **_kwargs: response)

    from open_stock_ai.storage.sqlite_store import SQLiteStore

    result, error = liquidity.fetch_official_share_revision(
        SQLiteStore(db_path=tmp_path / "liquidity.sqlite"), "2330.TW"
    )
    assert error is None
    assert result is not None
    assert guard.scopes == ["source:twse_openapi:twse_companies:share_revision"]


def test_twse_percentile_fetch_uses_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(percentiles, "default_external_transport_guard", lambda: guard)
    response = type("Response", (), {
        "url": "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU",
        "raise_for_status": lambda self: None,
        "json": lambda self: {"fields": ["日期", "本益比"], "data": [["115/08/26", "20"]]},
    })()
    monkeypatch.setattr(percentiles.httpx, "get", lambda *_args, **_kwargs: response)

    result = percentiles._fetch_twse_month("2330.TW", percentiles.date(2026, 8, 26))
    assert result["pe"] == 20.0
    assert guard.scopes == ["source:twse_official_web:valuation_history:2330"]


def test_mops_monthly_revenue_fetch_uses_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(monthly_revenue, "default_external_transport_guard", lambda: guard)
    monkeypatch.setattr(monthly_revenue, "urlopen", lambda *_args, **_kwargs: _Response(b""))
    monkeypatch.setattr(monthly_revenue, "parse_archive_html", lambda *args, **kwargs: [])

    page = monthly_revenue.fetch_archive_period("2025-01", market_segment="sii")
    assert page.items == []
    assert guard.scopes == [
        "source:mops:mops_monthly_revenue_archive:2025-01:sii::attempt:0"
    ]


def test_notification_provider_uses_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(notifications, "default_external_transport_guard", lambda: guard)
    monkeypatch.setattr(notifications, "urlopen", lambda *_args, **_kwargs: _Response(b""))

    result = notifications._execute_notification_request(
        "telegram",
        notifications.Request("https://api.telegram.org/bot/sendMessage"),
        "1234",
    )
    assert result.sent is True
    assert guard.scopes == ["notification:telegram"]


def test_realtime_fugle_quote_uses_async_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(realtime, "default_external_transport_guard", lambda: guard)
    response = type("Response", (), {"status_code": 401, "text": "unauthorized"})()
    monkeypatch.setattr(
        realtime.httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: _AsyncClient(response),
    )
    monkeypatch.setenv("FUGLE_MARKETDATA_API_KEY", "test-key")
    monkeypatch.setenv("REALTIME_QUOTE_PROVIDER", "fugle")
    from stock_ai.config import get_settings

    get_settings.cache_clear()
    try:
        import asyncio

        with pytest.raises(realtime.RealtimeProviderError, match="驗證失敗"):
            asyncio.run(realtime.fetch_fugle_quote("2330.TW"))
    finally:
        get_settings.cache_clear()
    assert guard.scopes == ["source:fugle_marketdata:quote:2330"]


def test_intraday_fugle_fetch_uses_async_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(candles, "default_external_transport_guard", lambda: guard)
    response = type("Response", (), {"status_code": 401, "text": "unauthorized"})()
    monkeypatch.setattr(
        candles.httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: _AsyncClient(response),
    )
    monkeypatch.setenv("FUGLE_MARKETDATA_API_KEY", "test-key")
    from stock_ai.config import get_settings

    get_settings.cache_clear()
    try:
        import asyncio

        with pytest.raises(candles.IntradayCandleError, match="授權失敗"):
            asyncio.run(
                candles.fetch_fugle_candles(
                    "2330.TW", trading_date="2026-07-24"
                )
            )
    finally:
        get_settings.cache_clear()
    assert guard.scopes == [
        "source:fugle_marketdata:intraday_candles:2330:2026-07-24"
    ]


def test_agent_web_search_uses_provider_transport_guards(monkeypatch, tmp_path):
    guard = _Guard()
    monkeypatch.setattr(general_tools, "default_external_transport_guard", lambda: guard)

    class SearchClient(_AsyncClient):
        async def get(self, url, **_kwargs):
            if "duckduckgo.com" in str(url):
                return type(
                    "Response",
                    (),
                    {
                        "status_code": 200,
                        "text": (
                            '<a class="result__a" href="//duckduckgo.com/l/?uddg='
                            'https%3A%2F%2Fexample.test%2Fone">One</a>'
                            '<div class="result__snippet">First</div>'
                        ),
                        "raise_for_status": lambda self: None,
                    },
                )()
            return type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "text": (
                        "<rss><channel><item><title>Two</title>"
                        "<link>https://example.test/two</link>"
                        "<description>Second</description></item></channel></rss>"
                    ),
                    "raise_for_status": lambda self: None,
                },
            )()

    monkeypatch.setattr(general_tools.httpx, "AsyncClient", lambda **_kwargs: SearchClient(None))
    provider = general_tools.GeneralAgentToolProvider(tmp_path)

    import asyncio

    result = asyncio.run(
        provider.execute(
            "web.search",
            {"query": "transport guard", "limit": 2},
            AgentRunContext(run_id="AR-web-search", autonomy="advisory", symbols=()),
        )
    )

    assert result["count"] == 2
    assert guard.scopes == [
        "tool:web.search:provider:duckduckgo_html",
        "tool:web.search:provider:bing_rss",
    ]


def test_short_daytrade_fetch_uses_endpoint_transport_guards(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(short_daytrade, "default_external_transport_guard", lambda: guard)

    class Response:
        status = 200
        url = "https://official.example/short-daytrade"

        def raise_for_status(self):
            return None

        def json(self):
            return {"date": "20260724", "tables": []}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(short_daytrade.httpx, "Client", Client)

    assert short_daytrade._fetch_date("2330.TW", short_daytrade.date(2026, 7, 24)) is None
    assert guard.scopes == [
        "source:twse:short_balance:20260724",
        "source:twse:daytrade:20260724",
        "source:twse:daily_volume:20260724",
    ]


def test_n8n_secure_request_uses_its_isolated_local_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(n8n, "_transport_guard_for", lambda _config: guard)
    monkeypatch.setattr(n8n, "_request_json", lambda *_args, **_kwargs: {"status": "ok"})
    executor = n8n.N8nGatewayExecutor(
        n8n.N8nGatewayConfig(gateway_url="http://127.0.0.1:5678")
    )

    assert executor._secure_request(
        "GET", "http://127.0.0.1:5678/healthz", {}, None, 5
    ) == {"status": "ok"}
    assert guard.scopes == [n8n._LOCAL_N8N_CONTROL_PLANE_SCOPE]


def test_google_news_fetch_uses_transport_guard(monkeypatch):
    guard = _Guard()
    monkeypatch.setattr(services, "default_external_transport_guard", lambda: guard)

    class Response:
        text = (
            "<rss><channel><item><title>TSMC event</title>"
            "<link>https://example.test/news</link>"
            "<pubDate>Wed, 20 Aug 2026 08:00:00 GMT</pubDate>"
            "<description>Verified event</description></item></channel></rss>"
        )

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(services.httpx, "Client", Client)

    result = services._fetch_google_news_events("2330.TW", limit=1)

    assert len(result) == 1
    assert result[0].title == "TSMC event"
    assert guard.scopes == ["source:google_news:events"]
