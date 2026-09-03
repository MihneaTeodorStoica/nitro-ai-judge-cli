# Nitro AI Judge CLI 3.1.5

3.1.5 adds Podman compatibility to Play manager installation and lifecycle commands. NAIJ now prefers a working local Podman installation with Compose and falls back to Docker when Podman is unavailable or unusable. Existing Docker installations continue to use their saved runtime.

Manager and competition image pulls no longer have arbitrary process or client-side deadlines. `--wait-timeout` continues to bound service readiness after images have been pulled, and interrupted pulls remain cancellable.

The default manager image is `ghcr.io/mihneateodorstoica/naij-play-manager:3.1.5`. Manager API v1 and the minimum compatible CLI version of 3.0.0 are unchanged.
