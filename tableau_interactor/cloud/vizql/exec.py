"""Python side of the VizQL bridge - inject dispatcher.js and run commands."""

import json
from pathlib import Path
from typing import Any

DISPATCHER_JS_PATH = Path(__file__).parent / "dispatcher.js"


def inject_dispatcher(page) -> dict:
    """Idempotent - load dispatcher.js into the page and return its handshake."""
    js = DISPATCHER_JS_PATH.read_text()
    # Wrap in eval-friendly IIFE return: dispatcher.js already returns its result.
    return page.evaluate(js)


def is_ready(page) -> bool:
    return bool(page.evaluate("() => !!(window.__tab && window.__tab.ready)"))


def info(page) -> dict:
    return page.evaluate(
        "() => window.__tab ? ({"
        " ready: window.__tab.ready, path: window.__tab.dispatcherPath,"
        " method: window.__tab.dispatcherMethod,"
        " datasourceId: window.__tab.datasourceId }) : { ready: false }"
    )


def send_command_raw(
    page,
    namespace: str,
    name: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_ms: int = 15000,
    opts: dict | None = None,
) -> dict:
    """Send a command and return the FULL raw server response, including
    vqlCmdResponse / layoutStatus / presentationLayerNotification. Use this
    when the data you need is stripped by the standard unwrap (e.g. dialog
    controllerIds buried in presentationLayerNotification)."""
    if not is_ready(page):
        h = inject_dispatcher(page)
        if not (h and h.get("ok")):
            return {"ok": False, "error": "dispatcher-injection-failed", "handshake": h}
    payload = {
        "namespace": namespace, "name": name,
        "params": params or {}, "opts": opts or {},
        "timeoutMs": timeout_ms,
    }
    js = """
    async ({namespace, name, params, opts, timeoutMs}) => {
      if (!window.__tab || !window.__tab.ready) return {ok: false, error: 'not-ready'};
      const p = window.__tab.sendCommandRaw(namespace, name, params, opts);
      const t = new Promise(res => setTimeout(() => res({ok: false, error: 'timeout', timeoutMs}), timeoutMs));
      return await Promise.race([p, t]);
    }
    """
    return page.evaluate(js, payload)


def send_command(
    page,
    namespace: str,
    name: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_ms: int = 15000,
    opts: dict | None = None,
) -> dict:
    """Invoke a VizQL command and wait for its callback. Returns {ok, result|error}.

    The injected sendCommand returns a Promise - Playwright's evaluate awaits it.
    """
    if not is_ready(page):
        h = inject_dispatcher(page)
        if not (h and h.get("ok")):
            return {"ok": False, "error": "dispatcher-injection-failed", "handshake": h}

    payload = {
        "namespace": namespace,
        "name": name,
        "params": params or {},
        "opts": opts or {},
        "timeoutMs": timeout_ms,
    }
    # Race the dispatcher promise against an explicit timeout so a hung command
    # surfaces as a clean failure rather than Playwright's generic timeout.
    js = """
    async ({namespace, name, params, opts, timeoutMs}) => {
      if (!window.__tab || !window.__tab.ready) return {ok: false, error: 'not-ready'};
      const p = window.__tab.sendCommand(namespace, name, params, opts);
      const t = new Promise(res => setTimeout(() => res({ok: false, error: 'timeout', timeoutMs}), timeoutMs));
      try {
        return await Promise.race([p, t]);
      } catch (e) {
        return e && typeof e === 'object' ? e : {ok: false, error: String(e)};
      }
    }
    """
    return page.evaluate(js, payload)


def set_datasource_id(page, datasource_id: str) -> None:
    """Override the auto-inferred datasource id (e.g., when multiple are connected)."""
    page.evaluate(
        "(id) => { if (window.__tab) window.__tab.datasourceId = id; }",
        datasource_id,
    )


def field_ref(page, field_name: str, role: str = "dimension") -> str:
    """Compute the fully-qualified field reference string used by drop-on-shelf."""
    return page.evaluate(
        "({name, role}) => window.__tab.fieldRef(name, role)",
        {"name": field_name, "role": role},
    )
