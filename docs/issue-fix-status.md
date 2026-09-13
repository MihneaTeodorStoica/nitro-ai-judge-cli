# 3.2.0 issue-fix status

Proposed in PR #92; no release publication or issue closure is implied.
“Locally verified” means the listed regression checks pass, not that every
platform, network failure, or release-publication scenario was exercised.

## Implemented and locally verified

| Issues | Result and coverage |
| --- | --- |
| #15, #16 | SPDX metadata, MIT/OFL texts, and font attribution; fresh wheel/sdist builds, Twine, and actual artifact-content checks pass. |
| #23, #60 | 64 KiB transfer reads, replayable file multipart bodies, disk-spooled/atomic downloads, response limits, raw stdout. Tests exercise a 64 MiB bounded sink, replay, changed files, truncation, redirect/error sinks, atomic failure, and large HTML pages. Real large-network/RSS benchmarking remains unperformed. |
| #39, #40 | Keyboard feedback scrolling and offline cached rows; existing Pilot regressions pass. |
| #41, #42, #43 | Scrollable Help/Download dialogs (including 70×20 Pilot coverage), visible archive warnings, proxy-required source validation before dismissal. |
| #44, #46 | Query-scoped submission cache identity/scoring mode; selection/detail consistency across rerender. |
| #45, #66 | Tilde expansion, native path normalization, bounded nonblocking suggestions; Pilot verifies Right acceptance and Tab focus. Windows mixed-separator regression fixed; confirmation awaits new remote CI. |
| #48 | Manager-wide inventory with image/workspace/health/container/operation columns, clean empty state, and no selected-contest requirement. |
| #51 | Schema-versioned read-only JSON for entity/context/Play/doctor commands; explicit task number/ID and submission mode. Checked-in JSON Schema, command-matrix validation, offline/auth/error/progress tests. |
| #56 | Detached mutations return accepted/reused operation IDs without waiting, and print exact-ID wait/cancel commands; parser, completion, and command regressions. |
| #58 | Read-only platform/config-source/permission/expiry diagnostics, bounded unauthenticated API and manager probes, Compose/context checks, aggregate exit status. Existing corrupt/offline/redaction/no-write tests pass. |
| #61 | Public OpenAPI 3.1 endpoint plus checked-in JSON; auth/CSRF/Origin, errors, 202 receipts, streams, and protocol enums. Validator, copy-equality, and complete registered-route parity tests pass. |
| #62 | Safe category discovery, cache-backed native completion, same-origin/task-scope validation, hardened filenames, per-download collision avoidance. |
| #63 | Authenticated bounded history with pagination, competition/action/status filters, timestamps/duration, bounded redacted failure summaries, and exact-ID detail/wait/cancel CLI. Store/API/client/CLI/auth/redaction tests pass. |
| #67 | TUI per-competition operation progress and exact-ID cancellation; Pilot covers progress plus completion/cancel race, and existing cancellation/shutdown regression passes. |
| #68 | Cancellable async log stream, follow/pause/resume, 2,000-line retention, conditional autoscroll and view-exit cleanup. Pilot covers bounded history, disconnect/retry, pause/resume and leaving Play. |
| #69, #70 | At most five primary context footer actions; pane/view Help. Local case-insensitive Overview search with F3/Shift+F3, match scrolling, no-match/escape behavior; normal and compact Pilot checks. |
| #71 | Confirmed final-selection controls with stale-selection guards; cancel/success/API-failure tests pass. |
| #72 | Deduplicated polling of selected pending submissions, stopped on selection/view/auth/context/app invalidation; existing selected-watch/view-exit/terminal-feedback tests pass. |

## External work or unverified release acceptance — keep open

| Issue | In this branch | Still required |
| --- | --- | --- |
| #13 | Idempotent GitHub Release creation/update after successful publication, using checked-in notes. Dry-run-first `scripts/backfill-releases.sh` verifies successful historical workflow SHAs. | Authorized historical backfill and a successful tagged release run. No remote releases were created by this pass. |
| #14 | Concrete `packaging/aur/PKGBUILD` and `.SRCINFO`, dependency/license/alias handoff. Shell syntax passes. | Publish upstream tag, regenerate metadata with Arch tooling, clean-chroot install/upgrade smoke, and AUR maintainer publication. |
| #38 | Edge workflow now targets `main`, not the deleted branch. | Merge and observe actual GHCR `edge` publication. PR publication jobs intentionally skip. |
| #64 | CI builds ARM64, checks executables/imports, then starts the real entrypoint and verifies health/identity under QEMU with scoped cleanup. | New remote ARM64 job must pass. Local host has rootless Podman and an existing manager, not the isolated Docker/QEMU CI environment; neither was modified for this check. |
| #65 | Visually reviewed real 120×34 TUI PNG, 10-second keyboard GIF, 1440×900 manager PNG; fictional fixtures, under 1 MiB combined. Reproducible offline capture script and future-tag README links. | Real 60–90 second notebook/runtime MP4, GitHub Release attachment, and post-tag GitHub/PyPI link/rendering verification. No fake end-to-end video is claimed. |

## Verification

- `python -m compileall -q src tests scripts`: passes.
- Full host unittest suite: **348 tests, four skips**, passing.
- OpenAPI validator and JSON Schema command-matrix checks ran (not skipped).
- Fresh 3.2.0 wheel/sdist build and Twine checks: pass.
- `NAIJ_VERIFY_DIST=1 python -m unittest tests.test_packaging -v`: three passes.
- `uv lock --check`, shell syntax, and `git diff --check`: pass.
- PNGs were opened for visual review; GIF duration/dimensions are generated
  explicitly by the capture script. These use fixtures, not live containers.

The ordinary-suite skips cover opt-in integration/artifact checks. Artifact
checks were subsequently run explicitly. Before this follow-up, remote native
Docker, Ubuntu/macOS, and build jobs passed, while Windows path assertions and
a CodeQL clear-text test fixture failed. Both findings are addressed here; the
new PR run is authoritative and remains to be checked after push.

Subagents supplied independent additions earlier in the pass. Further delegation
is blocked by the harness's hard delegation-tree request/agent limit; no attempt
was made to bypass it.
