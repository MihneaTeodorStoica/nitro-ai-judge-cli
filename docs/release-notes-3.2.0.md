# 3.2.0 — contest workflow improvements

Proposed minor release; manager API v1 and minimum CLI compatibility remain.

## Added

- Read-only `naij doctor` with configuration provenance, expiry/permission hints,
  bounded connectivity checks, and aggregate status.
- Schema-versioned `--json` for read-only entity, context, and Play commands.
- `naij play ls`, filtered/paginated operation history, exact-ID operation
  inspection/wait/cancellation, and detached mutations with `--detach`.
- Replayable streaming uploads, disk-spooled atomic downloads, raw stdout task
  downloads, response-size limits, and safe dynamic category completion.
- Checked-in and served OpenAPI 3.1 contract with schema/route parity tests.
- TUI filesystem suggestions, Overview match navigation, contextual footer/help,
  Play progress/cancellation, and follow/pause logs with bounded retention.
- Seeded real-UI screenshots and a ten-second keyboard GIF.

## Fixed

- Native path/tilde handling, proxy source validation, archive warnings,
  compact Help/Download dialogs, offline rows, selection/detail consistency,
  and selected pending-submission polling.
- Submission cache query identity and scoring-mode preservation.
- Confirmed set/unset-final controls with stale-selection guards.
- SPDX metadata, bundled font licenses, and development image publication branch.
- Windows mixed-path regression and clear-text diagnostic test fixture.

## Release tooling and remaining gates

- ARM64 build/tool/import checks now include real entrypoint health/identity
  under QEMU; the new remote job must pass before publication.
- GitHub Release creation is idempotent and uses checked-in release notes after
  successful package/container publication. Historical backfill remains manual.
- AUR recipe/metadata are supplied, but Arch chroot testing and maintainer
  publication remain outstanding.
- The full captioned notebook/runtime MP4 and its release attachment remain
  outstanding. Future-tag README images require post-publication verification.

Local verification: 348 host tests (four skips), schema validation, fresh builds,
Twine, and artifact license checks pass. No new release/tag was published.
See [issue-by-issue status](issue-fix-status.md) before closing issues.
