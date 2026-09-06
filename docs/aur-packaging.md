# AUR package handoff

The AUR package is community maintained. This is a proposed packaging contract
for an update, not a clean-chroot-tested package; it preserves both supported entry points.  Submit it
to the AUR maintainer rather than copying the current upstream checkout into a
system Python installation.

- Target the planned upstream tag, `v3.2.0`, after it is published (and advance
  `pkgver` with every upstream release).
- Keep `/usr/bin/naij` and `/usr/bin/nitro-cli`.  `nitro-cli` is deprecated but
  remains supported through 4.0.0; do not create a `naji` executable.
- `python-textual` mirrors the required `textual>=8.2,<9` dependency.
  `python-aiohttp` is optional for running the manager server directly. Normal
  host Play commands use the stdlib client; the container supplies its own aiohttp.
- Declare both distribution licenses: MIT for NAIJ and OFL-1.1 for the bundled
  Inter and Lexend font subsets.  Install the upstream `LICENSE`, `NOTICE`, and
  `LICENSES/` texts in `/usr/share/licenses`.
- Podman with a Compose provider is preferred for Play; Docker with the Compose
  plugin remains a fallback.  Neither runtime is needed for ordinary CLI use.

```PKGBUILD
# Maintainer: Răzvan <aurstuff@razv.xyz>
pkgname=nitro-ai-judge-cli
pkgver=3.2.0
pkgrel=1
pkgdesc="CLI client for judge.nitro-ai.org"
arch=('any')
url="https://github.com/MihneaTeodorStoica/nitro-ai-judge-cli"
license=('MIT' 'OFL-1.1')
depends=('python' 'python-textual')
makedepends=('git' 'python-build' 'python-installer' 'python-setuptools' 'python-wheel')
optdepends=(
  'python-aiohttp: required to run the manager server outside its container'
  'podman: preferred local container runtime for naij play'
  'podman-compose: Compose provider for Podman Play installations'
  'docker: fallback local container runtime for naij play'
  'docker-compose: Compose plugin for Docker Play installations'
)
source=("$pkgname::git+$url.git#tag=v$pkgver")
sha256sums=('SKIP')

build() {
  cd "$pkgname"
  python -m build --wheel --no-isolation
}

package() {
  cd "$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 NOTICE "$pkgdir/usr/share/licenses/$pkgname/NOTICE"
  install -Dm644 LICENSES/*.txt -t "$pkgdir/usr/share/licenses/$pkgname/LICENSES"
}
```

Before publishing, build in a clean Arch chroot and smoke-test:

```sh
naij --help
naij tui
nitro-cli --help
```

`nitro-cli --help` must display the documented deprecation warning and otherwise
match `naij --help`. Test Play separately with one listed container runtime installed.
