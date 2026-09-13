# Release media

Captured from the real UI using fictional offline fixtures:

- [tui-overview.png](tui-overview.png): 120×34 terminal cells, three panes.
- [tui-keyboard.gif](tui-keyboard.gif): 10 seconds of keyboard navigation,
  task views, and contextual help.
- [play-manager.png](play-manager.png): 1440×900, running/healthy,
  stopped/ready, and missing competitions; no browser chrome.

The combined PNG/GIF size is under 1 MiB (below the 3–5 MB budget).
No real credentials, usernames, paths, submissions, or registry digests appear.
The TUI's terminal frame is the real Textual SVG export, not a UI mockup.

## Reproduce

From an editable checkout with test dependencies:

```sh
python -m pip install pillow cairosvg playwright
python -m playwright install chromium
python scripts/capture-assets.py
```

The script uses temporary state and seeded API/backend fixtures, never live
Nitro accounts or Docker resources. The browser is restricted to the local
fixture server. This validates rendering, not a real container lifecycle.

README URLs are pinned to the proposed `v3.2.0` tag; they cannot resolve on
GitHub/PyPI until that release exists. The old `v3.0.0` tag must not be rewritten
just to add these newly captured assets.

## Still required before closing #65

Record a silent, captioned 60–90 second MP4 showing TUI navigation; a terminal
Play action appearing in the open dashboard; progress/logs/cancellation; opening
a real notebook; stop/restart; fallback recovery; Disconnect Nitro; and LAN Log
out. Use only fictional data and omit all credentials and private information.
Attach it to the matching GitHub Release, not to Git, and verify README rendering
and media links on both GitHub and PyPI after publication.
