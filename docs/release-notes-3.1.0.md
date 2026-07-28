# Nitro AI Judge CLI 3.1.0

3.1.0 adds the CLI and TUI work that landed since 3.0.4: credential controls and version output, task-data listing, cache/offline browsing, CLI routing and completion fixes, submission and context-safety improvements, and better Play-manager lifecycle handling.

Play-manager restarts are more robust too. The manager now migrates old Jupyter config mounts to `/etc/naij-jupyter`, restores the previous Compose definition if migration fails, and keeps local Jupyter POSTs working after a manager session restart while still rejecting foreign origins.

Delete workspace now preserves saved image metadata, so fallback images still render as ready instead of vanishing.

The Remove images action now deletes the currently selected fallback refs as well as the primary tags.

The default manager image is `ghcr.io/mihneateodorstoica/naij-play-manager:3.1.0`. Manager API v1 and the minimum compatible CLI version of 3.0.0 are unchanged.
