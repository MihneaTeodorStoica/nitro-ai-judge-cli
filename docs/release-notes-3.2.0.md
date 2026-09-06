# 3.2.0 — contest workflow improvements

Proposed minor release: new user-facing features, with manager API v1 and the
existing minimum CLI compatibility retained. No intentional breaking change.

## Added

- Read-only `naij doctor` local diagnostics.
- `naij play ls`, recent operation summaries through `naij play operations`, and
  detached Play mutations with `--detach`.
- Raw stdout downloads with `download-data -c CATEGORY -o -`.
- Safe additional task-file category keys.
- TUI inline path suggestions, local Overview search excerpts, contextual help,
  and confirmed set/unset-final controls.

## Fixed

- TUI tilde expansion, proxy source validation, visible archive warnings,
  scrollable Help/Download dialogs, offline submission rows, selection/detail
  consistency, and selected pending-submission polling.
- Submission cache query identity and scoring-mode preservation.
- Distribution SPDX metadata and bundled-font license/attribution files.
- Development manager image publication now follows `main`.

## Release tooling

- ARM64 manager image build/tool/import smoke before publication.
- GitHub Release creation after successful package/container publication.

## Known gaps

This branch does **not** complete every open issue. In particular, bounded-memory
file transfers, stable read-only JSON output, a complete OpenAPI contract, actual
release media, and full TUI Play progress/cancellation and log follow/pause remain
unimplemented. Several enhancements have only partial acceptance coverage.
See [the issue-by-issue status](issue-fix-status.md) before closing issues.
