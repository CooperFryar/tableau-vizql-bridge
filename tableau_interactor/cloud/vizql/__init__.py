"""Programmatic Tableau Cloud authoring via the internal VizQL command API.

Injects a small dispatcher into the live Tableau Cloud SPA and drives the same
internal `tabdoc`/`tabsrv` wire commands the UI fires - no official API required.

Layers:
  - `exec`    - the wire bridge: `send_command(page, namespace, name, params)`.
  - `api`     - ~80 typed primitives (drops, marks, filters, calc fields, params,
                dashboards, actions, analytics, formatting). Most code calls these.
  - `recipes` - composite chart builders atop `api`.

See `PROTOCOL.md` for the reverse-engineered wire facts and `API_REFERENCE.md`
(repo root) for the full primitive list.
"""
