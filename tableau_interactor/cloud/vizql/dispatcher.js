// vizql/dispatcher.js - finds Tableau's in-page command dispatcher by SIGNATURE
// (not by minified property name, which rotates each release) and exposes a stable
// API on window.__tab.
//
// Loaded into the page by exec.py. Idempotent - safe to re-inject.

(() => {
  // Always reinitialize on inject - cheap, and lets us pick up newly connected
  // datasources or react to dispatcher rotation between injections.

  // ---- Dispatcher discovery -------------------------------------------------

  // Two known entry points we'll look for (both observed in capture):
  //   executeSingleRemoteCommand(cmdObj, onSuccess, onError)  ← primary
  //   executeServerCommand(cmdObj, onSuccess, onError)        ← lower-level
  // We prefer executeSingleRemoteCommand because going through it inherits the
  // queue / sequence / UI-refresh logic the SPA expects.

  const PRIMARY = 'executeSingleRemoteCommand';
  const FALLBACK = 'executeServerCommand';

  function findDispatcher() {
    const visited = new WeakSet();
    const MAX_DEPTH = 6;
    let best = null;

    function walk(obj, path, depth) {
      if (best && best.method === PRIMARY) return;  // good enough
      if (depth > MAX_DEPTH || obj == null) return;
      const t = typeof obj;
      if (t !== 'object' && t !== 'function') return;
      if (visited.has(obj)) return;
      try { visited.add(obj); } catch (_) { return; }

      try {
        if (typeof obj[PRIMARY] === 'function') {
          best = { obj, method: PRIMARY, path };
          return;
        }
        if (!best && typeof obj[FALLBACK] === 'function') {
          best = { obj, method: FALLBACK, path };
        }
      } catch (_) {}

      let keys;
      try { keys = Object.keys(obj); } catch (_) { return; }
      if (keys.length > 200) keys = keys.slice(0, 200);
      for (const k of keys) {
        if (k.startsWith('_') && depth > 2) continue;
        let v;
        try { v = obj[k]; } catch (_) { continue; }
        if (v instanceof Node) continue;
        if (Array.isArray(v) && v.length > 50) continue;
        walk(v, path + '.' + k, depth + 1);
      }
    }

    // Start from the known-stable backdoor first, then fall back to a full walk.
    try {
      const targets = window.onerror && window.onerror._targets;
      if (targets && targets.length) {
        for (let i = 0; i < targets.length; i++) {
          walk(targets[i], 'window.onerror._targets.' + i, 0);
          if (best && best.method === PRIMARY) return best;
        }
      }
    } catch (_) {}

    if (!best || best.method !== PRIMARY) {
      walk(window, 'window', 0);
    }
    return best;
  }

  const found = findDispatcher();
  if (!found) {
    window.__tab = { ready: false, error: 'dispatcher not found' };
    return { ok: false, error: 'dispatcher not found' };
  }

  // ---- Public API -----------------------------------------------------------

  function rid() {
    return 'tabi-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  }

  // Stringify params Tableau-style: everything must be a string.
  function stringifyParams(params) {
    const out = {};
    for (const k of Object.keys(params || {})) {
      const v = params[k];
      if (v == null) { out[k] = ''; continue; }
      if (typeof v === 'string') { out[k] = v; continue; }
      if (typeof v === 'boolean' || typeof v === 'number') { out[k] = String(v); continue; }
      out[k] = JSON.stringify(v);
    }
    return out;
  }

  // Raw send - invokes the lower-level executor ($5) directly, returning the
  // FULL server response (with vqlCmdResponse / layoutStatus / presentationLayerNotification).
  // Use when you need data that the higher-level unwrap strips out.
  function sendCommandRaw(namespace, name, params, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      const cmd = {
        commandNamespace: namespace,
        commandName: name,
        commandParams: stringifyParams(params),
        // Default to TRUE so command failures come back through our promise
        // without popping Tableau's "Unexpected Server Error" modal in the UI.
        // Caller can override with opts.noExceptionDialog: false to see the dialog.
        noExceptionDialog: opts.noExceptionDialog !== undefined ? opts.noExceptionDialog : true,
        preserveRootResult: opts.preserveRootResult !== undefined ? opts.preserveRootResult : true,
        telemetryId: opts.telemetryId || rid(),
      };
      // The dispatcher's lower-level executor passes raw response to onSuccess.
      const lowerExecutor = found.obj.$5;
      if (!lowerExecutor || typeof lowerExecutor.executeServerCommand !== 'function') {
        resolve({ ok: false, error: 'no-lower-executor' });
        return;
      }
      lowerExecutor.executeServerCommand(cmd,
        function (raw) { resolve({ ok: true, raw }); },
        function (err) { resolve({ ok: false, error: String(err).slice(0, 600) }); });
    });
  }

  function sendCommand(namespace, name, params, opts) {
    opts = opts || {};
    return new Promise((resolve, reject) => {
      const cmd = {
        commandNamespace: namespace,
        commandName: name,
        commandParams: stringifyParams(params),
        // Default to TRUE so command failures come back through our promise
        // without popping Tableau's "Unexpected Server Error" modal in the UI.
        // Caller can override with opts.noExceptionDialog: false to see the dialog.
        noExceptionDialog: opts.noExceptionDialog !== undefined ? opts.noExceptionDialog : true,
        preserveRootResult: opts.preserveRootResult !== undefined ? opts.preserveRootResult : true,
        telemetryId: opts.telemetryId || rid(),
      };
      let settled = false;
      const onSuccess = (result) => {
        if (settled) return;
        settled = true;
        resolve({ ok: true, result });
      };
      const onError = (err) => {
        if (settled) return;
        settled = true;
        // Tableau errors are often Error objects with private _message / _innerException;
        // serialize to plain JSON so Playwright can ferry them across the bridge.
        const safe = { ok: false, error: 'command-error' };
        try {
          if (err == null) {
            // pass
          } else if (typeof err === 'string') {
            safe.message = err;
          } else if (err instanceof Error) {
            safe.message = err.message || String(err);
            safe.name = err.name;
            safe.stack = err.stack;
            // Tableau-specific private fields
            if (err._message) safe.tabMessage = err._message;
            if (err._error && err._error.message) safe.innerMessage = err._error.message;
          } else if (typeof err === 'object') {
            safe.message = err.message || err._message || JSON.stringify(err).slice(0, 500);
            if (err._message) safe.tabMessage = err._message;
          } else {
            safe.message = String(err);
          }
        } catch (_) {
          safe.message = 'unserializable error';
        }
        resolve(safe);  // resolve not reject - easier to consume on the Python side
      };
      try {
        found.obj[found.method](cmd, onSuccess, onError);
      } catch (e) {
        reject({ ok: false, error: 'invocation-threw', message: String(e), stack: e && e.stack });
      }
    });
  }

  // Helpers for the most common operations - composed atop sendCommand.

  // Find sqlproxy.<id> by walking the app graph and, as a fallback, scraping
  // any recently captured command payloads. Returns just the id portion.
  function inferDatasourceId() {
    const SQLPROXY = /sqlproxy\.([a-z0-9]{20,})/;
    const seen = new WeakSet();
    const hits = [];
    function walk(o, depth) {
      if (depth > 8 || o == null) return;
      const t = typeof o;
      if (t === 'string') {
        const m = o.match(SQLPROXY);
        if (m) hits.push(m[1]);
        return;
      }
      if (t !== 'object' && t !== 'function') return;
      if (seen.has(o)) return;
      try { seen.add(o); } catch (_) { return; }
      let keys;
      try { keys = Object.keys(o); } catch (_) { return; }
      for (const k of keys.slice(0, 80)) {
        let v;
        try { v = o[k]; } catch (_) { continue; }
        if (v instanceof Node) continue;
        if (Array.isArray(v) && v.length > 80) continue;
        walk(v, depth + 1);
        if (hits.length > 5) return;
      }
    }
    // Start from the app root (the object that owns the dispatcher), not from
    // the dispatcher itself - sibling objects often hold the data model.
    try {
      const targets = window.onerror && window.onerror._targets;
      if (targets && targets.length) walk(targets[0], 0);
    } catch (_) {}
    if (!hits.length) {
      try { walk(found.obj, 0); } catch (_) {}
    }
    // Fallback: pull from prior captured calls if the hunter is loaded.
    if (!hits.length && window.__vizqlHunt && window.__vizqlHunt.calls) {
      for (const c of window.__vizqlHunt.calls) {
        const blob = JSON.stringify(c.args || []);
        const m = blob.match(SQLPROXY);
        if (m) { hits.push(m[1]); break; }
      }
    }
    return hits[0] || null;
  }

  function fieldRef(name, role) {
    // role: 'dimension' | 'measure'
    const dsid = window.__tab && window.__tab.datasourceId;
    if (!dsid) throw new Error('datasourceId unknown - set window.__tab.datasourceId first');
    const meta = role === 'measure'
      ? { agg: 'sum', key: 'qk' }
      : { agg: 'none', key: 'nk' };
    return '[sqlproxy.' + dsid + '].[' + meta.agg + ':' + name + ':' + meta.key + ']';
  }

  window.__tab = {
    ready: true,
    dispatcher: found.obj,
    dispatcherPath: found.path,
    dispatcherMethod: found.method,
    datasourceId: inferDatasourceId(),
    sendCommand,
    sendCommandRaw,
    fieldRef,
    rid,
    stringifyParams,
  };

  return {
    ok: true,
    path: found.path,
    method: found.method,
    datasourceId: window.__tab.datasourceId,
  };
})();
