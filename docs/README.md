# 文件索引

本目錄只放長期有效的設計、資料政策、操作指南與整合說明。執行輸出、錯誤截圖和臨時除錯紀錄不得放入 `docs/`。

## Architecture

- [專案結構與維護定位](architecture/project-structure.md)
- [前端架構](architecture/frontend.md)
- [系統藍圖](architecture/system-blueprint.md)
- [可替換 Agent Runtime 與模擬交易](architecture/agent-trading.md)
- [Market Radar 多股票 Runtime 與成功契約](architecture/market-radar-runtime.md)
- [統一 Point-in-time 市場資料平台](architecture/unified-market-data-platform.md)
- [市場連動模型](architecture/market-linkage-model.md)

## Data

- [資料生態系](data/data-ecosystem.md)
- [TWSE OpenAPI 盤點](data/twse-openapi-inventory.md)
- [TWSE MIS 即時行情](data/free-realtime-twse-mis.md)
- [Realtime Quote V1 統一契約](data/realtime-quote-contract.md)
- [Intraday Candle V1 分 K 重建契約](data/intraday-candle-contract.md)
- [TDCC Holding Distribution History V1](data/tdcc-holding-history-contract.md)
- [授權行情來源](data/authorized-feed.md)
- [授權市場資料](data/authorized-market-data.md)
- [即時行情 UI 政策](data/realtime-ui-policy.md)

## Guides

- [專案協作、分支與提交指南](../CONTRIBUTING.md)
- [可攜式啟動](guides/portable-launch.md)
- [Apple Liquid Glass](guides/apple-liquid-glass.md)

## Integration

- [Open Stock AI](integration/open_stock_ai.md)
- [Open Stock AI Quickstart](integration/open_stock_ai_quickstart.md)
- [Codex Runtime](integration/codex_runtime.md)
- [外部來源 manifest](integration/external_sources_manifest.md)

## Migrations

- [Reconciliation Engine V1 / schema v23](migration/reconciliation-engine-v23.md)
- [Unified Data API V1](migration/unified-data-api-v1.md)
- [Complete Data Lineage V1 / schema v24](migration/complete-data-lineage-v24.md)
- [Design System](integration/design_system.md)

## Reference

- [查詢能力](reference/query-capabilities.md)
- [全面升級逐項追蹤與證據政策](refactor/comprehensive-upgrade-traceability.md)
