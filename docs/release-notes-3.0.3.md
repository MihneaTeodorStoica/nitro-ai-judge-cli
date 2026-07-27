# Nitro AI Judge CLI 3.0.3

3.0.3 moves the shared past-contest fallback notebook and submission proxy to
Nitro-owned, digest-pinned images. Contest-specific images still take
precedence, custom manager images remain unchanged, and shared fallbacks remain
protected from per-competition image deletion.

The notebook now receives `NITRO_SUBMISSION_PROXY_URL` alongside `PROXY_URL`;
`PROXY_URL_CLIENT` and the private proxy route are unchanged.

Existing environments saved by 3.0.2 keep their current images on `start` and
`restart`. Their next `pull`, `play`, or `recreate` rewrites Compose state to
the new shared fallbacks when contest-specific images are unavailable. Old
Docker Hub images are not removed automatically. With `--pull never`, the new
digests must already be cached or the saved environment remains unchanged.

The default manager image is
`ghcr.io/mihneateodorstoica/naij-play-manager:3.0.3`. Manager API v1 and the
minimum compatible CLI version of 3.0.0 are unchanged.
