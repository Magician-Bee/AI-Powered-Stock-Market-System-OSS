from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec

from stock_ai.network_security import url_has_blocked_target, validate_public_http_url

_MAX_TEXT = 80_000
_MAX_LINKS = 100
_BLOCKED_INPUT_TYPES = {"file", "hidden", "password"}
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _BrowserSession:
    playwright: Any
    browser: Any
    context: Any
    page: Any


class BrowserToolProvider:
    """Host-owned stateful browser tools shared by every Agent driver.

    Browser output never contains cookies, storage, passwords, hidden fields or
    file contents. Navigation and reading are advisory-safe; page mutation needs
    the same explicit elevated autonomy used for other external operations.
    """

    provider_id = "browser"

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = (project_root or Path(__file__).resolve().parents[3]).resolve()
        self.runtime_root = self.project_root / ".runtime" / "agent-browser"
        self.storage_state_path = self.runtime_root / "storage-state.json"
        self._sessions: dict[str, _BrowserSession] = {}
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="browser.open",
                    description=(
                        "Open a real external page in an isolated Playwright browser. Use this for JavaScript-rendered "
                        "pages that web.fetch cannot read; localhost and private-network targets are blocked."
                    ),
                    category="browser",
                    packages=("Playwright", "system Chrome/Edge/Chromium"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["url"],
                        "properties": {
                            "url": {"type": "string", "minLength": 1, "maxLength": 4000},
                            "wait_until": {
                                "type": "string",
                                "enum": ["commit", "domcontentloaded", "load", "networkidle"],
                            },
                            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
                        },
                    },
                ),
                AgentToolSpec(
                    name="browser.read_page",
                    description="Read the current rendered page title, URL, visible text, links and non-secret controls.",
                    category="browser",
                    packages=("Playwright",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "max_chars": {"type": "integer", "minimum": 1000, "maximum": _MAX_TEXT},
                            "include_links": {"type": "boolean"},
                            "include_controls": {"type": "boolean"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="browser.click",
                    description=(
                        "Click one visible element in the current external page. This may change remote state and "
                        "therefore requires explicit external_execute/full_execute autonomy."
                    ),
                    category="browser",
                    mutating=True,
                    requires_external_execution=True,
                    packages=("Playwright",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["selector"],
                        "properties": {
                            "selector": {"type": "string", "minLength": 1, "maxLength": 1000},
                            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
                        },
                    },
                ),
                AgentToolSpec(
                    name="browser.fill",
                    description=(
                        "Fill one visible non-secret form control. Password, hidden and file inputs are always blocked; "
                        "requires explicit external_execute/full_execute autonomy."
                    ),
                    category="browser",
                    mutating=True,
                    requires_external_execution=True,
                    packages=("Playwright",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["selector", "value"],
                        "properties": {
                            "selector": {"type": "string", "minLength": 1, "maxLength": 1000},
                            "value": {"type": "string", "maxLength": 20_000},
                            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
                        },
                    },
                ),
                AgentToolSpec(
                    name="browser.wait_for",
                    description="Wait for a rendered selector to become attached, visible, hidden or detached.",
                    category="browser",
                    packages=("Playwright",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["selector"],
                        "properties": {
                            "selector": {"type": "string", "minLength": 1, "maxLength": 1000},
                            "state": {
                                "type": "string",
                                "enum": ["attached", "detached", "visible", "hidden"],
                            },
                            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
                        },
                    },
                ),
                AgentToolSpec(
                    name="browser.close",
                    description="Close this run's isolated browser session and persist only browser storage state locally.",
                    category="browser",
                    packages=("Playwright",),
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                ),
            )
        }

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def describe(self) -> dict[str, Any]:
        package_ready = _playwright_available()
        executable = _browser_executable()
        return {
            "configured": package_ready,
            "runtime_ready": package_ready and executable is not None,
            "health": "ready" if package_ready and executable else ("browser_missing" if package_ready else "dependency_missing"),
            "engine": "playwright",
            "browser_executable": str(executable) if executable else None,
            "active_session_count": len(self._sessions),
            "private_network_blocked": True,
            "credential_fields_blocked": True,
            "downloads_blocked": True,
            "host_execution_binding": True,
        }

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"Unknown browser capability: {name}")
        if spec.requires_external_execution and not context.allow_external_actions:
            raise PermissionError(f"{name} requires autonomy=external_execute or full_execute")
        if name == "browser.close":
            return await self.close_run(context.run_id)
        if name == "browser.open":
            return await self._open(arguments, context)
        session = await self._session(context.run_id)
        if name == "browser.read_page":
            return await self._read(session, arguments)
        if name == "browser.click":
            return await self._click(session, arguments)
        if name == "browser.fill":
            return await self._fill(session, arguments)
        if name == "browser.wait_for":
            return await self._wait_for(session, arguments)
        raise RuntimeError(f"Browser capability is registered but not implemented: {name}")

    async def close_run(self, run_id: str) -> dict[str, Any]:
        session = self._sessions.pop(run_id, None)
        if session is None:
            return {"schema_version": "open_stock_ai.browser_close.v1", "closed": False, "run_id": run_id}
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        try:
            await session.context.storage_state(path=str(self.storage_state_path))
            self.storage_state_path.chmod(0o600)
        except Exception as exc:
            logger.warning(
                "Browser storage state could not be persisted for run %s: %s",
                run_id,
                exc,
            )
        try:
            await session.context.close()
        finally:
            try:
                await session.browser.close()
            finally:
                await session.playwright.stop()
        return {"schema_version": "open_stock_ai.browser_close.v1", "closed": True, "run_id": run_id}

    async def _session(self, run_id: str) -> _BrowserSession:
        existing = self._sessions.get(run_id)
        if existing is not None:
            return existing
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed; reinstall project dependencies") from exc
        executable = _browser_executable()
        if executable is None:
            raise RuntimeError("No supported Chrome, Edge or Chromium executable is installed")
        runtime = await async_playwright().start()
        browser = await runtime.chromium.launch(
            headless=True,
            executable_path=str(executable),
            args=["--disable-sync", "--disable-extensions", "--no-first-run", "--no-default-browser-check"],
        )
        storage_state = str(self.storage_state_path) if self.storage_state_path.is_file() else None
        context = await browser.new_context(
            accept_downloads=False,
            storage_state=storage_state,
            viewport={"width": 1440, "height": 1000},
            locale="zh-TW",
        )

        async def block_unsafe_route(route: Any) -> None:
            try:
                blocked = await asyncio.to_thread(
                    _url_has_blocked_target,
                    route.request.url,
                    resolve_dns=True,
                )
            except (RuntimeError, ValueError):
                blocked = True
            if blocked:
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

        await context.route("**/*", block_unsafe_route)
        page = await context.new_page()
        session = _BrowserSession(playwright=runtime, browser=browser, context=context, page=page)
        self._sessions[run_id] = session
        return session

    async def _open(self, arguments: dict[str, Any], context: AgentRunContext) -> dict[str, Any]:
        url = _validate_external_url(arguments.get("url"))
        session = await self._session(context.run_id)
        wait_until = str(arguments.get("wait_until") or "domcontentloaded")
        timeout_ms = max(1, min(int(arguments.get("timeout_seconds") or 30), 60)) * 1000
        response = await session.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        final_url = _validate_external_url(session.page.url)
        return {
            "schema_version": "open_stock_ai.browser_navigation.v1",
            "requested_url": url,
            "url": final_url,
            "title": await session.page.title(),
            "status_code": response.status if response is not None else None,
            "rendered": True,
            "javascript_enabled": True,
            "session_owner": "host",
        }

    async def _read(self, session: _BrowserSession, arguments: dict[str, Any]) -> dict[str, Any]:
        max_chars = max(1000, min(int(arguments.get("max_chars") or 30_000), _MAX_TEXT))
        text = await session.page.locator("body").inner_text(timeout=15_000)
        result: dict[str, Any] = {
            "schema_version": "open_stock_ai.browser_page.v1",
            "url": session.page.url,
            "title": await session.page.title(),
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
            "rendered": True,
            "secret_values_returned": False,
        }
        if arguments.get("include_links", True):
            result["links"] = (await session.page.locator("a[href]").evaluate_all(
                "els => els.slice(0, 100).map((el, index) => ({index, text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 300), href: el.href}))"
            ))[:_MAX_LINKS]
        if arguments.get("include_controls", True):
            result["controls"] = await session.page.locator("button, select, textarea, input:not([type=password]):not([type=file]):not([type=hidden])").evaluate_all(
                "els => els.slice(0, 100).map((el, index) => ({index, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), name: el.name || null, id: el.id || null, label: (el.innerText || el.getAttribute('aria-label') || el.placeholder || '').trim().slice(0, 300)}))"
            )
        return result

    async def _click(self, session: _BrowserSession, arguments: dict[str, Any]) -> dict[str, Any]:
        selector = _selector(arguments.get("selector"))
        timeout_ms = max(1, min(int(arguments.get("timeout_seconds") or 15), 30)) * 1000
        locator = session.page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout_ms)
        metadata = await locator.evaluate(
            "el => ({tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 300)})"
        )
        if str(metadata.get("type") or "").casefold() in _BLOCKED_INPUT_TYPES:
            raise PermissionError("Browser interactions with password, hidden and file inputs are blocked")
        before_url = session.page.url
        await locator.click(timeout=timeout_ms)
        await session.page.wait_for_timeout(150)
        return {
            "schema_version": "open_stock_ai.browser_action.v1",
            "action": "click",
            "selector": selector,
            "element": metadata,
            "before_url": before_url,
            "url": session.page.url,
            "title": await session.page.title(),
            "remote_state_may_have_changed": True,
        }

    async def _fill(self, session: _BrowserSession, arguments: dict[str, Any]) -> dict[str, Any]:
        selector = _selector(arguments.get("selector"))
        value = str(arguments.get("value") or "")
        timeout_ms = max(1, min(int(arguments.get("timeout_seconds") or 15), 30)) * 1000
        locator = session.page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout_ms)
        metadata = await locator.evaluate(
            "el => ({tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), name: el.name || null, id: el.id || null})"
        )
        if str(metadata.get("type") or "").casefold() in _BLOCKED_INPUT_TYPES:
            raise PermissionError("Browser interactions with password, hidden and file inputs are blocked")
        await locator.fill(value, timeout=timeout_ms)
        return {
            "schema_version": "open_stock_ai.browser_action.v1",
            "action": "fill",
            "selector": selector,
            "element": metadata,
            "characters_written": len(value),
            "value_returned": False,
            "remote_state_may_have_changed": False,
        }

    async def _wait_for(self, session: _BrowserSession, arguments: dict[str, Any]) -> dict[str, Any]:
        selector = _selector(arguments.get("selector"))
        state = str(arguments.get("state") or "visible")
        timeout_ms = max(1, min(int(arguments.get("timeout_seconds") or 30), 60)) * 1000
        await session.page.locator(selector).first.wait_for(state=state, timeout=timeout_ms)
        return {
            "schema_version": "open_stock_ai.browser_wait.v1",
            "selector": selector,
            "state": state,
            "matched": True,
            "url": session.page.url,
        }


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def _browser_executable() -> Path | None:
    configured = str(os.getenv("STOCK_AI_BROWSER_EXECUTABLE") or "").strip()
    candidates = [
        configured,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome") or "",
        shutil.which("microsoft-edge") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
    ]
    for value in candidates:
        if value:
            path = Path(value).expanduser().resolve()
            if path.is_file() and os.access(path, os.X_OK):
                return path
    return None


def _selector(value: Any) -> str:
    selector = str(value or "").strip()
    if not selector:
        raise ValueError("Browser action requires selector")
    if any(token in selector.casefold() for token in ("type=password", "type='password'", 'type="password"')):
        raise PermissionError("Password selectors are blocked")
    return selector


def _validate_external_url(value: Any) -> str:
    return validate_public_http_url(value, purpose="Browser")


def _url_has_blocked_target(url: str, *, resolve_dns: bool) -> bool:
    return url_has_blocked_target(url, resolve_dns=resolve_dns)
