# R-004 Hosted OpenTelemetry tracing 驗收

## 結果

- Requirement：`R-004`
- 候選 commit：`e6b76f12ac36acd518318cc6fd60f1d142c4dbee`
- GitHub Actions run：[`34117518277`](https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34117518277)
- `collector-delivery` job：`101727686160`
- `clean-runner-verify` job：`101727872951`
- Gate：`passed=true`，`blockers=[]`
- 排程：每週一 `05:23 UTC`，亦支援 `workflow_dispatch`

第一台 GitHub-hosted Linux runner 啟動固定版本 `otel/opentelemetry-collector-contrib:0.160.0`，透過真實 OTLP/HTTP JSON receiver 接收 trace，再由 Collector file exporter 寫入 JSON lines。第二台乾淨 runner 下載第一份 artifact，重新驗證 candidate identity、Collector image/config、來源 trace、delivery receipt、Collector 輸出及父子鏈，才產生總 gate receipt。

OpenTelemetry 官方 OTLP 規格要求 OTLP/HTTP JSON 使用 HTTP POST、`application/json`、lowerCamelCase 欄位、hex trace/span ID 與整數 enum；本實作依此格式送出。Collector contrib 的官方 file exporter 說明則定義 `format: json` 時每行是一個 JSON object。本次 pinned image 的啟動 log 顯示 `service.version=0.160.0`、OTLP HTTP receiver 監聽 `4318`，並完成正常 shutdown。

官方規格：

- [OTLP Specification](https://opentelemetry.io/docs/specs/otlp/)
- [OpenTelemetry Collector contrib file exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/fileexporter)

## 跨服務 trace

- Trace ID：`6a5b0c190acd43c82737d588d364135d`
- `request`：service `stock-ai-api`，span `4d0130fa38f84eb2`，無外部 parent
- `decision`：service `stock-ai-decision`，span `1f9a566cd4814639`，parent `4d0130fa38f84eb2`
- `order`：service `stock-ai-order`，span `5dbecbf31d5d4605`，parent `1f9a566cd4814639`
- 訂單屬性固定為 `order.mode=paper`，本次沒有實盤下單、帳戶呼叫或模型執行。

## Artifact 與雜湊

兩份 artifact 都保留至 `2026-12-06T11:36:50Z`：

- `stock-ai-otel-trace-e6b76f12ac36acd518318cc6fd60f1d142c4dbee`：artifact `10016862965`
- `stock-ai-otel-verification-e6b76f12ac36acd518318cc6fd60f1d142c4dbee`：artifact `10016883075`

下載後重新驗證：

- delivery receipt SHA-256：`67cc5d0969c2af82cf00931896eb898677a6473f0299aab1270479e214a61c03`
- hosted gate receipt SHA-256：`4e387e838bb7424cd633e3e243612dcde5e776dbb986f2a3b683c98d3e7bfeed`
- Collector output SHA-256：`d0644f80b4258e4df690bfcc2626faab1b09a5e4359dd7b05aa16f3c24d872a9`
- Collector config SHA-256：`dfd93d7059bc0a8b3672b8edaebc29e6b31626f5f467fdd05387ad327b9f8615`
- Collector image ID：`sha256:5f92255513d9fcfe89bedd02c1a244717bcde223827465031744d5ffdd8f0b2d`
- gate artifact file SHA-256：`c37cc11cd5e93c5d5285a48421edf133631037827354a16ae18e6c2a0a7843dc`
- clean-runner verification file SHA-256：`301464106a7f10c3b7a9191297bee4ec362fe9c32e0d5e876e59cf409c027aa4`

## 本機與原生桌面驗收

本機相關回歸測試最終為 `16 passed`。指定啟動器以分支 commit `e6b76f12ac36` 啟動，`驗證目前執行版本.command` 顯示 managed service source 與 served UI SHA 一致，結果為 `VERIFIED CURRENT PROJECT INSTANCE`。

Computer Use 在可見的原生 **Stock AI Liquid Glass** 視窗中操作首頁、系統／資料平台、Agent Dock 收合與重新展開。首頁市場資料、系統資料平台與 Dock 都正常顯示；沒有操作 Agent Run 控制，也沒有送出任何 Agent 訊息或模型請求。

![R-004 原生桌面資料平台與 Agent Dock 驗收](native-r004-otel-tracing-20260907.png)
