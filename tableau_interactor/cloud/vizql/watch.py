"""Long-running capture: arm a comprehensive hunter, leave it running so the user
can perform a real manual action. Reporter dumps everything captured.

Workflow:
  1. `python -m tableau_interactor.cloud.vizql.watch arm`
       → enhanced hunter installed; script exits, page state persists
  2. user performs the action manually
  3. `python -m tableau_interactor.cloud.vizql.watch report`
       → dumps requests, responses, dispatcher calls, subscriber fires
"""

import json
import sys
import time
from .connect import connect_to_workbook_page


ARM_JS = r"""
(() => {
  // Tear down any prior install
  const prior = window.__vizqlWatch;
  if (prior) {
    try {
      if (prior.origFetch) window.fetch = prior.origFetch;
      if (prior.origXhrOpen) XMLHttpRequest.prototype.open = prior.origXhrOpen;
      if (prior.origXhrSend) XMLHttpRequest.prototype.send = prior.origXhrSend;
      for (const w of prior.wrappers || []) {
        try { if (w.obj[w.name] && w.obj[w.name].__orig) w.obj[w.name] = w.obj[w.name].__orig; } catch {}
      }
    } catch {}
  }

  const w = {
    startedAt: Date.now(),
    xhrs: [],          // {ts, method, url, reqBody, status, respText, respHeaders}
    fetches: [],       // {ts, url, init, status, respText}
    calls: [],         // dispatcher method invocations
    fires: [],         // events fired on response/exception listeners
    wrappers: [],
    notes: [],
  };
  window.__vizqlWatch = w;

  const summarize = (v, d) => {
    d = d || 0;
    if (d > 3) return '<deep>';
    if (v == null) return v;
    const t = typeof v;
    if (t === 'string') return v.length > 500 ? v.slice(0, 500) + '…' : v;
    if (t === 'number' || t === 'boolean') return v;
    if (t === 'function') return '<fn ' + (v.name || '') + ' len=' + String(v).length + '>';
    if (Array.isArray(v)) return v.slice(0, 10).map(x => summarize(x, d + 1));
    if (t === 'object') {
      const o = {}; let keys;
      try { keys = Object.keys(v).slice(0, 30); } catch { return '<unreadable>'; }
      o.__ctor = v.constructor && v.constructor.name;
      for (const k of keys) try { o[k] = summarize(v[k], d + 1); } catch { o[k] = '<err>'; }
      return o;
    }
    return String(v);
  };

  // ---- 1. XHR wrap with response capture ----
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  w.origXhrOpen = origOpen;
  w.origXhrSend = origSend;

  XMLHttpRequest.prototype.open = function (method, url) {
    this.__wMethod = method;
    this.__wUrl = url;
    this.__wOpened = Date.now() - w.startedAt;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const entry = {
      ts: Date.now() - w.startedAt,
      method: this.__wMethod || 'POST',
      url: this.__wUrl || '',
      isCommand: (this.__wUrl || '').includes('/commands/'),
      reqBodyType: body && body.constructor && body.constructor.name,
      reqBodyLen: body && body.length,
      stackTop: (new Error()).stack.split('\n').slice(1, 6).join('\n'),
    };
    // Capture multipart body fields if FormData
    if (body && body.entries && typeof body.entries === 'function') {
      entry.reqFields = {};
      try {
        for (const [k, v] of body.entries()) {
          entry.reqFields[k] = (typeof v === 'string') ? (v.length > 600 ? v.slice(0, 600) + '…' : v) : '<blob>';
        }
      } catch {}
    } else if (typeof body === 'string') {
      entry.reqBody = body.length > 1500 ? body.slice(0, 1500) + '…' : body;
    }
    w.xhrs.push(entry);
    const idx = w.xhrs.length - 1;
    const orig = this;
    this.addEventListener('readystatechange', function () {
      if (orig.readyState === 4) {
        try {
          w.xhrs[idx].status = orig.status;
          w.xhrs[idx].respLen = orig.responseText ? orig.responseText.length : 0;
          if (orig.responseText) {
            const t = orig.responseText;
            w.xhrs[idx].respText = t.length > 6000 ? t.slice(0, 6000) + '…[+' + (t.length - 6000) + ']' : t;
          }
          w.xhrs[idx].respHeaders = (orig.getAllResponseHeaders() || '').slice(0, 400);
          w.xhrs[idx].doneAt = Date.now() - w.startedAt;
        } catch (e) { w.xhrs[idx].captureErr = String(e); }
      }
    });
    return origSend.apply(this, arguments);
  };

  // ---- 2. fetch wrap with response cloning ----
  const origFetch = window.fetch;
  w.origFetch = origFetch;
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const entry = {
      ts: Date.now() - w.startedAt,
      url,
      isCommand: url.includes('/commands/'),
      method: (init && init.method) || 'GET',
      stackTop: (new Error()).stack.split('\n').slice(1, 6).join('\n'),
    };
    w.fetches.push(entry);
    const idx = w.fetches.length - 1;
    return origFetch.apply(this, arguments).then(resp => {
      try {
        const clone = resp.clone();
        w.fetches[idx].status = resp.status;
        clone.text().then(t => {
          w.fetches[idx].respText = t.length > 6000 ? t.slice(0, 6000) + '…[+' + (t.length - 6000) + ']' : t;
          w.fetches[idx].doneAt = Date.now() - w.startedAt;
        }).catch(() => {});
      } catch {}
      return resp;
    });
  };

  // ---- 3. Wrap dispatcher methods AND every successor method, to see the call chain ----
  function wrap(obj, name, label) {
    if (!obj || typeof obj[name] !== 'function' || obj[name].__orig) return;
    const orig = obj[name];
    const wrapper = function () {
      w.calls.push({
        ts: Date.now() - w.startedAt,
        label, name,
        argc: arguments.length,
        args: Array.prototype.slice.call(arguments).map(a => summarize(a, 0)),
        stack: (new Error()).stack.split('\n').slice(1, 10).join('\n'),
      });
      return orig.apply(this, arguments);
    };
    wrapper.__orig = orig;
    obj[name] = wrapper;
    w.wrappers.push({ obj, name });
  }

  try {
    const targets = window.onerror && window.onerror._targets;
    if (targets && targets.length) {
      const root = targets[0];
      // Recursively wrap any function whose name matches likely dispatcher patterns,
      // OR which is on $Y / $Y.$5 (our known dispatcher chain).
      const dispatcher = root.$Y;
      if (dispatcher) {
        const proto = Object.getPrototypeOf(dispatcher);
        for (const name of Object.getOwnPropertyNames(proto)) {
          try { if (typeof dispatcher[name] === 'function') wrap(dispatcher, name, 'WebCommandHandler.' + name); } catch {}
        }
        // Also wrap the lower-level executor on $5
        if (dispatcher.$5) {
          const p2 = Object.getPrototypeOf(dispatcher.$5);
          for (const name of Object.getOwnPropertyNames(p2)) {
            try { if (typeof dispatcher.$5[name] === 'function') wrap(dispatcher.$5, name, '$5.' + name); } catch {}
          }
        }
        // Subscribe to event chains to see what fires after responses
        try {
          dispatcher.add_onRemoteCommandResponse(function (seq, cmd, resp) {
            let cmdName, respKeys;
            try { cmdName = cmd && cmd.commandName; } catch {}
            try { respKeys = resp && Object.keys(resp).slice(0, 25); } catch {}
            w.fires.push({ kind: 'response', ts: Date.now() - w.startedAt, seq, cmdName, respKeys });
          });
          dispatcher.add_onRemoteCommandException(function (seq, cmd, err) {
            w.fires.push({ kind: 'exception', ts: Date.now() - w.startedAt, seq, cmdName: cmd && cmd.commandName, err: String(err).slice(0, 200) });
          });
        } catch {}
      }
    }
  } catch (e) { w.armError = String(e); }

  return {
    armed: true,
    wrappedCount: w.wrappers.length,
    dispatcher: window.onerror && window.onerror._targets && window.onerror._targets[0] ? 'found' : 'missing',
  };
})();
"""


REPORT_JS = r"""
() => {
  const w = window.__vizqlWatch;
  if (!w) return { error: 'not armed' };
  return {
    startedAt: w.startedAt,
    armSecondsAgo: ((Date.now() - w.startedAt) / 1000).toFixed(1),
    xhrs: w.xhrs,
    fetches: w.fetches,
    calls: w.calls,
    fires: w.fires,
    notes: w.notes,
    armError: w.armError || null,
  };
}
"""


def arm(page) -> dict:
    return page.evaluate(ARM_JS)


def report(page) -> dict:
    return page.evaluate(REPORT_JS)


def main_arm():
    pw, page = connect_to_workbook_page()
    try:
        print(f"Page: {page.url}")
        h = arm(page)
        print(json.dumps(h, indent=2))
        print("\nWatcher armed. Go perform the action. When done, run:")
        print("    .venv/bin/python -m tableau_interactor.cloud.vizql.watch report")
    finally:
        pw.stop()


def main_report():
    pw, page = connect_to_workbook_page()
    try:
        r = report(page)
        out = "tableau_cloud_state/watch_report.json"
        with open(out, "w") as f:
            json.dump(r, f, indent=2, default=str)
        if r.get("error"):
            print(f"ERROR: {r['error']}")
            return

        print(f"Window: {r['armSecondsAgo']}s")
        print(f"XHRs: {len(r['xhrs'])}   fetches: {len(r['fetches'])}   dispatcher calls: {len(r['calls'])}   event fires: {len(r['fires'])}")
        print()
        print("=== /commands/ requests (with response status) ===")
        for x in r["xhrs"]:
            if not x.get("isCommand"):
                continue
            cmd = (x.get("reqFields", {}) or {}).get("commandName") or "?"
            print(f"  [{x['ts']:>5}→{x.get('doneAt','?'):>5}ms] {x['method']} {x['url'].split('/commands/')[-1]}  status={x.get('status')}  reqlen={x.get('reqBodyLen','?')}  resplen={x.get('respLen','?')}")
        print()
        print("=== dispatcher method calls (in order) ===")
        for c in r["calls"]:
            arg0 = c["args"][0] if c["args"] else None
            cmd = arg0.get("commandName") if isinstance(arg0, dict) else "?"
            print(f"  [{c['ts']:>5}ms] {c['label']}  cmd={cmd}")
        print()
        print("=== response/exception events fired ===")
        for f in r["fires"]:
            print(f"  [{f['ts']:>5}ms] {f['kind']}  seq={f.get('seq')} cmd={f.get('cmdName')} respKeys={f.get('respKeys')}")
        print()
        print(f"Full payloads dumped to {out}")
    finally:
        pw.stop()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "arm"
    if cmd == "arm":
        main_arm()
    elif cmd == "report":
        main_report()
    else:
        print(f"unknown command: {cmd}")
        sys.exit(2)
