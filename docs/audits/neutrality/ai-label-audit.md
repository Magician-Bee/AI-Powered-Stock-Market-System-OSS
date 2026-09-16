# AI Label Audit

- Baseline commit: `94e8cb2d65c9f5d8f8de04717847d77c513cee27`
- Generated at: `2026-07-29T19:18:01.417729+00:00`
- Scope: Production `src/` and `config/`; vendored UI files excluded.
- Matches: **1**

## Purpose

Find UI/API labels that could claim AI participation without a successful model invocation receipt.

## Classification notes

- MODEL/HYBRID labels require provider, model ID, call ID, and success status.
- Deterministic outputs must use Rule/Quant wording.

## Inventory

- `src/stock_ai/services.py:1055` — `extra_limitations=["本頁為非模型規則與多來源彙整，不代表 AI 判斷、成功機率或投資報酬。"],`
