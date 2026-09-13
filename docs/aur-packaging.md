# AUR package handoff

The AUR package is community maintained. This branch supplies a concrete
[PKGBUILD](../packaging/aur/PKGBUILD) and
[.SRCINFO](../packaging/aur/.SRCINFO) for the proposed `v3.2.0` tag.
They are **not clean-chroot tested or published to AUR**. Do not publish a
recipe pointing at a tag that does not yet exist.

- Preserve both `/usr/bin/naij` and deprecated `/usr/bin/nitro-cli` (supported
  through 4.0.0). There is no `naji` executable.
- Mirror mandatory `textual>=8.2,<9` and `aiohttp>=3.10,<4` dependencies.
  The latter supports cancellable TUI log streaming as well as the manager.
- Package MIT and OFL-1.1 licenses, `NOTICE`, and all bundled font notices.
- Recommend Podman/Compose for Play; retain Docker/Compose as an alternative.
  Neither container runtime is needed for ordinary CLI use.
- The source is pinned to an upstream Git tag; `SKIP` is the conventional VCS
  checksum setting, not permission to substitute an unreviewed source.

After the upstream release, the AUR maintainer should regenerate `.SRCINFO`
with `makepkg --printsrcinfo`, check its diff, build in a clean Arch chroot,
and run:

```sh
naij --help
naij tui
nitro-cli --help
```

Verify the alias warning, installed dependencies, fonts, and license files.
Test Play separately with one supported runtime. This handoff changes no
external AUR repository and does not install into the host system Python.
