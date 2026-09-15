"""Phase 16 — alert recipients, kept OUT of the tracked/public config.

The email-alert recipients are personal PII, so they live in a git-ignored
data/alerts.local.json (not in config.json, which is committed and — on GitHub Pages —
public). merged() overlays that local file on top of config's alerts block; callers
still let an ALERT_TO / ALERT_CC env var win over both.

data/alerts.local.json shape:  {"to": "you@example.com", "cc": ["a@x.com", "b@y.com"]}
"""

import json
from pathlib import Path


def merged(cfg, root) -> dict:
    """The alerts config with recipients from data/alerts.local.json overlaid (if present)."""
    a = dict((cfg or {}).get("alerts", {}) or {})
    try:
        local = json.loads((Path(root) / "data" / "alerts.local.json").read_text())
        if isinstance(local, dict):
            a.update({k: v for k, v in local.items() if v not in (None, "", [])})
    except (OSError, json.JSONDecodeError):
        pass
    return a
