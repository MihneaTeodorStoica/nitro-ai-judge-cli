# Nitro AI Judge CLI 3.0.2

3.0.2 keeps long Play progress labels inside the terminal row, preventing
digest-pinned fallback pulls from flooding narrow terminals. Spinner cleanup
and the non-cancelling Ctrl-C status/cancel guidance remain unchanged.

`naij play manager start` now installs or repairs a missing manager instead of
erroring, while still starting an existing stopped manager normally. Running
competitions in the Play dashboard also have a **Copy link** action beside
**Open**.

The default manager image is
`ghcr.io/mihneateodorstoica/naij-play-manager:3.0.2`. Manager API v1 and the
minimum compatible CLI version of 3.0.0 are unchanged.
