from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "src" / "stock_ai" / "ui" / "static" / "js" / "features" / "agent"


def test_stream_controller_reconnects_from_last_durable_sequence_without_cancelling_run():
    source = (AGENT / "agent-stream-controller.js").read_text(encoding="utf-8")

    assert "after_sequence=${last}" in source
    assert "last_sequence_by_run[runId]" in source
    assert "500 * (2 **" in source
    assert "Math.min(15000" in source
    assert "AbortController" in source
    assert "generation !== this.generation" in source
    assert "payload.event.run_id !== runId" in source
    assert "runId !== this.runId" in source
    assert "this.store.dispatch(payload.event)" in source
    assert "/cancel" not in source


def test_stream_controller_poll_reconciles_webkit_stalls_without_duplicate_execution():
    source = (AGENT / "agent-stream-controller.js").read_text(encoding="utf-8")

    assert "scheduleReconcile(this.generation)" in source
    assert "/events?after_sequence=${last}" in source
    assert ".sort((a, b) => Number(a.sequence || 0) - Number(b.sequence || 0))" in source
    assert ".forEach(event => this.store.dispatch(event))" in source
    assert "'max_steps_reached'" in source
    assert "'partially_completed'" in source
    assert "['completed', 'partially_completed', 'max_steps_reached', 'failed', 'cancelled', 'interrupted'].includes(status)" in source
    assert "Polling is a WebKit" in source


def test_stream_controller_reopens_after_clean_eof_when_run_is_not_terminal():
    source = (AGENT / "agent-stream-controller.js").read_text(encoding="utf-8")

    assert "const terminal = await this.consume" in source
    assert "!terminal" in source
    assert "this.store.set({ connection_state: 'reconnecting' })" in source
    assert "return this.open(generation)" in source


def test_snapshot_hydration_replays_events_before_following_live_stream():
    source = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    store = (AGENT / "agent-dock-store.js").read_text(encoding="utf-8")

    assert "/snapshot" in source
    assert "snapshot.events.forEach(event => store.dispatch(event))" in source
    assert "store.applySnapshot(snapshot)" in source
    assert "return this.open(generation)" in (AGENT / "agent-stream-controller.js").read_text(
        encoding="utf-8"
    )
    assert "last_sequence_by_run" in store
