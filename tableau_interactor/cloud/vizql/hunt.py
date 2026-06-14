"""Dispatcher hunter - find the in-page object that owns SendCommand.

Strategy:
  1. `arm()` injects a script that wraps fetch/XHR and walks the global graph for
     anything resembling SendCommand / executeNextRemoteCommand / sendAjaxRequest.
     Results land on `window.__vizqlHunt`.
  2. User manually performs ONE simple action in the UI (e.g. drag a field to Rows).
  3. `report()` reads `window.__vizqlHunt` and prints what was captured.

Run end-to-end:
    .venv/bin/python -m tableau_interactor.cloud.vizql.hunt
"""

import json
import sys
import time

from .connect import connect_to_workbook_page


# Injected into the page. Kept self-contained so it can be reloaded without
# polluting global helpers.
HUNT_SCRIPT = r"""
(() => {
  // Idempotent: tear down any prior wrappers/state before re-arming.
  const prior = window.__vizqlHunt;
  if (prior) {
    try {
      // Unwrap previously wrapped dispatcher methods.
      for (const w of (prior.wrappers || [])) {
        try {
          if (w.obj && w.obj[w.method] && w.obj[w.method].__vizqlOrig) {
            w.obj[w.method] = w.obj[w.method].__vizqlOrig;
          }
        } catch (_) {}
      }
      // Restore fetch / XHR.
      if (prior.origFetch) window.fetch = prior.origFetch;
      if (prior.origXhrOpen) XMLHttpRequest.prototype.open = prior.origXhrOpen;
      if (prior.origXhrSend) XMLHttpRequest.prototype.send = prior.origXhrSend;
    } catch (_) {}
  }

  const hunt = {
    armed: true,
    armedAt: Date.now(),
    commands: [],        // {ts, url, namespace, name, method, stack}
    candidates: [],      // {path, methods, ctor}
    calls: [],           // {ts, path, method, args, signature, stack}
    wrappers: [],        // {obj, method} for teardown
    wrappedCount: 0,
  };
  window.__vizqlHunt = hunt;

  // Cheap serializer - avoids cycles, truncates strings, names functions.
  function summarize(v, depth) {
    if (depth === undefined) depth = 0;
    if (depth > 3) return '<deep>';
    if (v === null) return null;
    if (v === undefined) return undefined;
    const t = typeof v;
    if (t === 'string') return v.length > 200 ? v.slice(0, 200) + '…' : v;
    if (t === 'number' || t === 'boolean') return v;
    if (t === 'function') {
      const src = String(v);
      return '<function ' + (v.name || 'anon') + ' len=' + src.length + '>';
    }
    if (Array.isArray(v)) {
      return v.slice(0, 10).map(x => summarize(x, depth + 1));
    }
    if (t === 'object') {
      const out = {};
      let keys;
      try { keys = Object.keys(v).slice(0, 25); } catch (_) { return '<unreadable>'; }
      out.__ctor = (v.constructor && v.constructor.name) || null;
      for (const k of keys) {
        try { out[k] = summarize(v[k], depth + 1); } catch (_) { out[k] = '<err>'; }
      }
      return out;
    }
    return String(v);
  }

  // --- 1. Walk the global object graph for SendCommand-bearing objects ---
  const TARGET_METHODS = new Set([
    'SendCommand', 'sendCommand',
    'executeNextRemoteCommand', 'executeSingleRemoteCommand', 'executeServerCommand',
    'sendAjaxRequest',
  ]);
  const SEEN = new WeakSet();
  const MAX_DEPTH = 5;
  const MAX_KEYS_PER_LEVEL = 200;

  function walk(obj, path, depth) {
    if (depth > MAX_DEPTH) return;
    if (obj === null || obj === undefined) return;
    const t = typeof obj;
    if (t !== 'object' && t !== 'function') return;
    if (SEEN.has(obj)) return;
    try { SEEN.add(obj); } catch (_) { return; }

    let keys;
    try { keys = Object.keys(obj); } catch (_) { return; }
    if (keys.length > MAX_KEYS_PER_LEVEL) keys = keys.slice(0, MAX_KEYS_PER_LEVEL);

    // Check own methods first
    const hits = [];
    for (const m of TARGET_METHODS) {
      try {
        if (typeof obj[m] === 'function') hits.push(m);
      } catch (_) {}
    }
    if (hits.length) {
      hunt.candidates.push({
        path,
        methods: hits,
        ownKeys: keys.slice(0, 30),
        ctor: (obj.constructor && obj.constructor.name) || null,
      });
    }

    // Recurse - but skip obvious DOM nodes and big arrays to keep this bounded
    for (const k of keys) {
      if (k.startsWith('_') && depth > 2) continue;
      let v;
      try { v = obj[k]; } catch (_) { continue; }
      if (v instanceof Node) continue;
      if (Array.isArray(v) && v.length > 50) continue;
      walk(v, path + '.' + k, depth + 1);
    }
  }
  try { walk(window, 'window', 0); } catch (e) { hunt.walkError = String(e); }

  // --- 1b. Wrap every discovered candidate method so we see how it is called ---
  // This is the key signal: what arguments does executeSingleRemoteCommand take?
  function resolvePath(path) {
    const parts = path.split('.').slice(1);  // drop leading 'window'
    let obj = window;
    for (const p of parts) {
      if (obj == null) return null;
      try { obj = obj[p]; } catch (_) { return null; }
    }
    return obj;
  }
  for (const cand of hunt.candidates) {
    const obj = resolvePath(cand.path);
    if (!obj) continue;
    for (const m of cand.methods) {
      try {
        const orig = obj[m];
        if (typeof orig !== 'function' || orig.__vizqlWrapped) continue;
        const wrapper = function () {
          try {
            hunt.calls.push({
              ts: Date.now() - hunt.armedAt,
              path: cand.path,
              method: m,
              argc: arguments.length,
              args: Array.prototype.slice.call(arguments).map(a => summarize(a, 0)),
              stack: (new Error()).stack,
            });
          } catch (_) {}
          return orig.apply(this, arguments);
        };
        wrapper.__vizqlWrapped = true;
        wrapper.__vizqlOrig = orig;
        obj[m] = wrapper;
        hunt.wrappers.push({ obj: obj, method: m });
        hunt.wrappedCount++;
      } catch (_) {}
    }
  }

  // --- 2. Wrap fetch so we capture every /commands/ call with a stack ---
  const origFetch = window.fetch;
  hunt.origFetch = origFetch;
  window.fetch = function patchedFetch(input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (url.includes('/commands/')) {
        const m = url.match(/\/commands\/([^/]+)\/([^/?#]+)/);
        hunt.commands.push({
          via: 'fetch',
          ts: Date.now() - hunt.armedAt,
          url,
          namespace: m ? m[1] : null,
          name: m ? m[2] : null,
          method: (init && init.method) || 'GET',
          stack: (new Error()).stack,
        });
      }
    } catch (_) {}
    return origFetch.apply(this, arguments);
  };

  // --- 3. Wrap XHR.open/send the same way ---
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  hunt.origXhrOpen = origOpen;
  hunt.origXhrSend = origSend;
  XMLHttpRequest.prototype.open = function patchedOpen(method, url) {
    this.__vizqlMethod = method;
    this.__vizqlUrl = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function patchedSend(body) {
    try {
      const url = this.__vizqlUrl || '';
      if (url.includes('/commands/')) {
        const m = url.match(/\/commands\/([^/]+)\/([^/?#]+)/);
        hunt.commands.push({
          via: 'xhr',
          ts: Date.now() - hunt.armedAt,
          url,
          namespace: m ? m[1] : null,
          name: m ? m[2] : null,
          method: this.__vizqlMethod || 'POST',
          stack: (new Error()).stack,
        });
      }
    } catch (_) {}
    return origSend.apply(this, arguments);
  };

  return {
    armed: true,
    candidateCount: hunt.candidates.length,
    candidatePreview: hunt.candidates.slice(0, 10).map(c => ({ path: c.path, methods: c.methods })),
    wrappedCount: hunt.wrappedCount,
  };
})();
"""


REPORT_SCRIPT = r"""
(() => {
  const h = window.__vizqlHunt;
  if (!h) return { error: 'not armed' };
  return {
    armed: h.armed,
    candidateCount: h.candidates.length,
    candidates: h.candidates,
    commandCount: h.commands.length,
    commands: h.commands,
    callCount: h.calls.length,
    calls: h.calls,
    walkError: h.walkError || null,
  };
})();
"""


def arm(page) -> dict:
    """Inject the hunting script. Returns the initial walk result."""
    return page.evaluate(HUNT_SCRIPT)


def report(page) -> dict:
    """Pull the captured commands + global-walk candidates back out of the page."""
    return page.evaluate(REPORT_SCRIPT)


def main() -> int:
    pw, page = connect_to_workbook_page()
    try:
        print(f"Connected to: {page.url}")
        print("Arming hunter…")
        armed = arm(page)
        print(json.dumps(armed, indent=2)[:2000])

        print()
        print("Now perform ONE simple action in the browser:")
        print("  → drag any field from the Data pane onto the Rows shelf.")
        print()
        input("Press Enter here when the drop is complete and the pill is visible… ")

        # Small grace period for trailing refresh commands
        time.sleep(1.0)

        result = report(page)
        # Save full result for offline inspection
        out_path = "tableau_cloud_state/dispatcher_hunt.json"
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\nFull report written to {out_path}")

        print(f"\n=== {result['commandCount']} command(s) captured ===")
        for c in result["commands"]:
            print(f"  [{c['ts']:>5}ms] {c['method']} /{c['namespace']}/{c['name']}  ({c['via']})")

        print(f"\n=== {result['candidateCount']} dispatcher candidate(s) ===")
        for cand in result["candidates"][:15]:
            print(f"  {cand['path']}  methods={cand['methods']}  ctor={cand.get('ctor')}")

        if result["candidateCount"] == 0:
            print("\n(No global-graph hits. The dispatcher is probably inside a closure -")
            print(" we'll fall back to parsing the captured stack traces in dispatcher_hunt.json.)")

        return 0
    finally:
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
