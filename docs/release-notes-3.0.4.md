# Nitro AI Judge CLI 3.0.4

3.0.4 automatically migrates an installed older official Play manager before
the next Play command, TUI launch, `manager install`, `manager start`, or
`manager restart`. The CLI pulls the 3.0.4 manager image and recreates only the
manager container while preserving its endpoint, TLS configuration, private
state, competition workspaces, and saved environments.

Custom manager images remain pinned. A failed migration restores the previous
manager configuration and Compose definition instead of leaving a partial
upgrade.

Manager API v1, the minimum compatible CLI version of 3.0.0, and the 3.0.3
digest-pinned contestant runtime fallbacks are unchanged.
