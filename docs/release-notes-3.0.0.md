# Nitro AI Judge CLI 3.0.0

3.0 adds the keyboard-first Textual TUI and the Docker-backed Play manager.
The manager dashboard now follows NAIJ operations and manager-owned Docker
changes live, retains cached rows through refresh failures, exposes operation
progress and redacted logs, and provides safe offline start instructions.

Use **Disconnect Nitro** to remove only the Judge credentials saved by the
manager. On LAN installations, **Log out** expires only the current browser
session; other browsers, CLI credentials, and workspaces remain unchanged.

Install with `pipx install nitro-ai-judge-cli`, then run `naij tui` or install
the local runtime with `naij play manager install`. If the dashboard fallback
page appears, run `naij play manager start` on the machine hosting the manager
and retry the page.
