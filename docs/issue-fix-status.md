# Local issue-fix pass

This is an **incomplete implementation proposed for 3.2.0**, not a claim that all
GitHub acceptance criteria are satisfied. The branch is being submitted for review;
no release publication or issue closure is implied. Issue numbers refer to
`MihneaTeodorStoica/nitro-ai-judge-cli`.

## Changes with local verification

| Issue | Local result |
| --- | --- |
| #13 | Workflow creates a GitHub Release after successful GHCR/PyPI publication. Historical releases are not backfilled. |
| #15 | PEP 639 SPDX expression and license-file metadata added; wheel/sdist contents verified. |
| #16 | Inter/Lexend OFL texts and font attribution included in distributions. |
| #38 | `edge` publication follows `main` instead of the deleted feature branch. CI has not run remotely. |
| #39 | Already implemented in the starting checkout; existing keyboard-detail-scroll Pilot test passes. |
| #40 | Offline submission rendering no longer discards cached rows for lack of a session username. |
| #41 | Download and Help use scrollable modal containers. Full minimum-size acceptance still needs dedicated coverage. |
| #42 | Download warnings are shown instead of a success-only status. |
| #43 | Proxy-mode submission form requires source before dismissal. |
| #44 | Query-scoped caches retain author/page/page-size/mode, canonical cache is protected, cached records preserve scoring mode, legacy complete-only labels infer complete scores. |
| #45 | TUI download/submission paths expand `~`; relative paths and spaces remain intact. |
| #46 | Rerender preserves selected ID/mode and loaded detail instead of resetting the highlight to the first row. |
| #48 | `naij play ls` lists managed environments without contest context. Further column/filter requirements remain to be checked. |
| #56 | Mutating Play commands accept `--detach`, print the operation ID and follow-up commands, and do not wait or falsely report completion. `--open` conflicts with detach. |
| #60 | `download-data -c CATEGORY -o -` writes raw bytes without extraction; authentication/download diagnostics go to stderr. It still buffers the response (see #23). |
| #66 | TUI path fields offer bounded, nonblocking inline filesystem suggestions; Right accepts at the end, Tab still changes focus. Pilot acceptance coverage is still limited. |
| #71 | Contextual final-selection button and `f` action require confirmation, use the shared API helper, and guard stale selection. Tests cover cancel, success, and API failure. |
| #72 | Selected pending submissions resume one deduplicated watcher. Selection/view/authentication invalidation stops it. Tests cover deduplication and view-exit cleanup; the full acceptance matrix is not yet covered. |

## Partial work — do not close these issues yet

| Issue | Implemented | Remaining |
| --- | --- | --- |
| #14 | AUR maintainer handoff in `docs/aur-packaging.md`. | Real PKGBUILD/.SRCINFO update in the AUR repository and clean Arch chroot testing. |
| #58 | Read-only `naij doctor`: redacted API URL, state/credential/config presence, permissions, bounded Compose probes; no migration/chmod/login. | Credential freshness and fuller live manager identity/health diagnostics. |
| #62 | Safe new category keys can be discovered/requested; traversal-style keys rejected and output filenames hardened. | Dynamic native completion and complete malicious-link/collision/fallback coverage. |
| #63 | Bounded recent-operation summary endpoint, client, and `naij play operations --limit N`. | Filtering/pagination and dashboard integration. |
| #64 | ARM64 image build and executable/import smoke tests added before publication. | Actual ARM64 manager startup/health/integration smoke and CI execution. |
| #69 | Help text describes the current pane/view and correct confirmation/path behavior. | Fully context-sensitive footer bindings and keymap coverage. |
| #70 | Overview `/` performs local case-insensitive search and displays counts/excerpts. | Next/previous navigation, scrolling matches into view, dedicated Pilot coverage. |

## Not implemented

- #23: bounded-memory uploads/downloads and replay-safe streaming transport.
- #51: stable JSON output contract for all read-only commands.
- #61: complete, valid OpenAPI contract and route/schema parity tests.
- #65: actual screenshots and keyboard recording. Synthetic mockups were not retained.
- #67: complete TUI Play progress/cancellation controls.
- #68: TUI Play log follow/pause mode.

## Verification

- Full host unittest suite: **329 tests, four skips**, passing on this host.
- `python -m build`: wheel and sdist build successfully.
- `python -m twine check dist/*`: passes.
- `NAIJ_VERIFY_DIST=1 python -m unittest tests.test_packaging -v`: three passing checks, including actual packaged licenses/fonts.
- No Docker integration, ARM64 runtime, Arch chroot, or remote CI run was performed.

The skipped tests include opt-in Docker integration and the ordinary-suite
artifact check; the latter was also run explicitly against freshly built artifacts.
