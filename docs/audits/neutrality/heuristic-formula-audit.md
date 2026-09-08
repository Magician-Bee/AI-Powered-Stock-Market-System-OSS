# Heuristic Formula Audit

- Baseline commit: `94e8cb2d65c9f5d8f8de04717847d77c513cee27`
- Generated at: `2026-07-29T19:18:01.417729+00:00`
- Scope: Production `src/` and `config/`; vendored UI files excluded.
- Matches: **49**
- Formulas with Method ID: **49**

Every retained rule or compatibility projection below has a stable Method ID. These IDs describe deterministic methods; they do not claim model inference or calibrated probability.

| Method ID | Location | Formula / field |
|---|---|---|
| `host.provider_decision_schema_validation.v1` | `src/open_stock_ai/agent_runtime/orchestrator.py:2539` | `confidence = float(decision["confidence"])` |
| `host.multi_intent_hint_weights.v1` | `src/open_stock_ai/agent_runtime/routing.py:70` | `confidence=1.0 if task_kind_hint == "market_radar" else 0.45,` |
| `host.multi_intent_hint_weights.v1` | `src/open_stock_ai/agent_runtime/routing.py:78` | `confidence=0.8,` |
| `host.multi_intent_hint_weights.v1` | `src/open_stock_ai/agent_runtime/routing.py:86` | `confidence=0.78,` |
| `host.multi_intent_hint_weights.v1` | `src/open_stock_ai/agent_runtime/routing.py:94` | `confidence=0.82,` |
| `host.multi_intent_hint_weights.v1` | `src/open_stock_ai/agent_runtime/routing.py:116` | `confidence=1.0,` |
| `host.multi_intent_hint_weights.v1` | `src/open_stock_ai/agent_runtime/routing.py:132` | `confidence=1.0 if supplied_symbol_source == "workflow_parameter" else 0.55,` |
| `host.multi_intent_hint_weights.v1` | `src/open_stock_ai/agent_runtime/routing.py:205` | `confidence = max(0.0, min(float(item.get("confidence") or 0.0), 1.0))` |
| `host.multi_intent_hint_weights.v1` | `src/open_stock_ai/agent_runtime/routing.py:211` | `confidence=confidence,` |
| `open_stock_ai.legacy_workspace_projection.v1` | `src/open_stock_ai/agent_workspace.py:100` | `confidence=1.0 if str(symbol or "").strip() else 0.0,` |
| `external.fingpt_deterministic_forecast_projection.v1` | `src/open_stock_ai/external_sources/fingpt_source.py:153` | `bin_label = "up by 1-2%" if forecast_score < 0.55 else "up by 2-3%"` |
| `external.fingpt_deterministic_forecast_projection.v1` | `src/open_stock_ai/external_sources/fingpt_source.py:156` | `bin_label = "down by 1-2%" if forecast_score > -0.55 else "down by 2-3%"` |
| `external.finrobot_rule_valuation_projection.v1` | `src/open_stock_ai/external_sources/finrobot_source.py:216` | `target_price = price * (1 + growth * 0.45)` |
| `external.finrobot_rule_valuation_projection.v1` | `src/open_stock_ai/external_sources/finrobot_source.py:217` | `low_estimate = target_price * 0.92` |
| `external.finrobot_rule_valuation_projection.v1` | `src/open_stock_ai/external_sources/finrobot_source.py:218` | `high_estimate = target_price * 1.08` |
| `external.finrobot_rule_valuation_projection.v1` | `src/open_stock_ai/external_sources/finrobot_source.py:219` | `if target_price >= price * 1.08:` |
| `external.finrobot_rule_valuation_projection.v1` | `src/open_stock_ai/external_sources/finrobot_source.py:221` | `elif target_price <= price * 0.92:` |
| `research.legacy_confidence_attribution_projection.v1` | `src/open_stock_ai/research/portfolio_attribution.py:46` | `confidence = self._number(item.get("confidence"))` |
| `risk.fixed_stop_reference.v1` | `src/open_stock_ai/risk/stop_loss.py:4` | `def fixed_stop_loss(entry_price: float, loss_pct: float) -> float:` |
| `strategy.weighted_rule_score.v2` | `src/open_stock_ai/strategy/strategy_engine.py:80` | `confidence=round(abs(rule_score), 3) if data_ready else None,` |
| `strategy.weighted_rule_score.v2` | `src/open_stock_ai/strategy/strategy_engine.py:90` | `target_price=structured_decision.target_price if data_ready else None,` |
| `strategy.weighted_rule_score.v2` | `src/open_stock_ai/strategy/strategy_engine.py:91` | `stop_loss=structured_decision.stop_loss if data_ready else None,` |
| `strategy.fixed_reference_multiplier.v1` | `src/open_stock_ai/strategy/structured_decision.py:66` | `target_price = _target_price(entry_price, rating)` |
| `strategy.fixed_reference_multiplier.v1` | `src/open_stock_ai/strategy/structured_decision.py:67` | `stop_loss = _stop_loss(entry_price, rating)` |
| `strategy.fixed_reference_multiplier.v1` | `src/open_stock_ai/strategy/structured_decision.py:75` | `target_price=target_price,` |
| `strategy.fixed_reference_multiplier.v1` | `src/open_stock_ai/strategy/structured_decision.py:76` | `stop_loss=stop_loss,` |
| `strategy.fixed_reference_multiplier.v1` | `src/open_stock_ai/strategy/structured_decision.py:96` | `if score >= 0.55:` |
| `strategy.fixed_reference_multiplier.v1` | `src/open_stock_ai/strategy/structured_decision.py:100` | `if score <= -0.55:` |
| `strategy.fixed_reference_multiplier.v1` | `src/open_stock_ai/strategy/structured_decision.py:120` | `PortfolioRating.OVERWEIGHT: 1.08,` |
| `strategy.fixed_reference_multiplier.v1` | `src/open_stock_ai/strategy/structured_decision.py:131` | `PortfolioRating.BUY: 0.92,` |
| `strategy.fixed_reference_multiplier.v1` | `src/open_stock_ai/strategy/structured_decision.py:134` | `PortfolioRating.SELL: 1.08,` |
| `stock_ai.rule_radar_wait_window.v1` | `src/stock_ai/codex_market.py:358` | `wait_days = max(1, min(10, math.ceil((0.55 - min(abs(score), 0.55)) * 12)))` |
| `audit.retained_formula.src.stock_ai.data_platform.warehouse.py.v1` | `src/stock_ai/data_platform/warehouse.py:286` | `confidence=excluded.confidence,` |
| `audit.retained_formula.src.stock_ai.data_platform.warehouse.py.v1` | `src/stock_ai/data_platform/warehouse.py:1219` | `confidence=excluded.confidence,` |
| `audit.retained_formula.src.stock_ai.market_radar.py.v1` | `src/stock_ai/market_radar.py:46` | `confidence = float(value)` |
| `stock_ai.notification_priority_rule.v1` | `src/stock_ai/mvp_features.py:111` | `return 0.92` |
| `stock_ai.official_event_match_score.v1` | `src/stock_ai/official_events.py:365` | `confidence=0.92,` |
| `stock_ai.entity_match_score.v1` | `src/stock_ai/realtime_data.py:178` | `confidence=0.45,` |
| `stock_ai.deterministic_service_rules.v1` | `src/stock_ai/services.py:247` | `confidence=confidence,` |
| `stock_ai.deterministic_service_rules.v1` | `src/stock_ai/services.py:260` | `("/news/eventList", "news", "twse-event", 0.55),` |
| `stock_ai.deterministic_service_rules.v1` | `src/stock_ai/services.py:279` | `confidence=confidence,` |
| `stock_ai.deterministic_service_rules.v1` | `src/stock_ai/services.py:310` | `confidence=confidence,` |
| `stock_ai.deterministic_service_rules.v1` | `src/stock_ai/services.py:351` | `confidence=0.4,` |
| `stock_ai.deterministic_service_rules.v1` | `src/stock_ai/services.py:704` | `confidence = 0.35` |
| `stock_ai.deterministic_service_rules.v1` | `src/stock_ai/services.py:710` | `confidence = 0.55` |
| `stock_ai.deterministic_service_rules.v1` | `src/stock_ai/services.py:716` | `confidence = 0.5` |
| `stock_ai.deterministic_service_rules.v1` | `src/stock_ai/services.py:720` | `return LinkageExplanation(source=source, target=target_symbol, direction=direction, confidence=confidence, mechanism=mechanism, path=path, evidence=evidence, affected_symbols=[])` |
| `ui.rule_reference_range_render.v1` | `src/stock_ai/ui/static/js/features/codex.js:200` | `<span class="decision-levels">${levelLabel} ${item.price == null ? '-' : escapeHtml(niceNumber(item.price))} · 區間目標 ${item.target_price == null ? '-' : escapeHtml(niceNumber(item.target_price))} · 規則停損 ${item.stop_loss == null ? '-' : escapeHtml(niceNumber(item.stop_loss))}</span>` |
| `ui.quant_rule_signal_render.v1` | `src/stock_ai/ui/static/js/features/quant-research.js:477` | `<p>進場 ${snapshot.price === null || snapshot.price === undefined ? '-' : niceNumber(signal.entry_price ?? snapshot.price)} / 目標 ${signal.target_price === null || signal.target_price === undefined ? '-' : niceNumber(signal.target_price)} / 停損 ${signal.stop_loss === null || signal.stop_loss === undefined ? '-' : niceNumber(signal.stop_loss)}</p>` |
