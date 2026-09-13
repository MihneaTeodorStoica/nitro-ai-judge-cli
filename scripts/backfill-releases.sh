#!/usr/bin/env bash
# Defaults to a dry run. Requires git tags locally and gh auth with contents:write.
set -euo pipefail
REPO=MihneaTeodorStoica/nitro-ai-judge-cli
APPLY=false
if [[ ${1:-} == --apply ]]; then APPLY=true; elif [[ $# -gt 0 ]]; then echo 'usage: backfill-releases.sh [--apply]' >&2; exit 2; fi
for pair in v3.0.1:30275043653 v3.0.2:30277099896; do
  TAG=${pair%%:*}; RUN=${pair##*:}
  NOTES="docs/release-notes-${TAG#v}.md"
  test -f "$NOTES"
  SHA=$(git rev-parse "$TAG^{commit}")
  ACTUAL=$(gh api "repos/$REPO/actions/runs/$RUN" --jq 'select(.conclusion == "success") | .head_sha')
  if [[ $SHA != "$ACTUAL" ]]; then echo "Successful publication could not be verified for $TAG" >&2; exit 1; fi
  if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then echo "$TAG already exists; leaving it unchanged"; continue; fi
  echo "Verified $TAG; would create release using $NOTES (without replacing latest)"
  if $APPLY; then gh release create "$TAG" --repo "$REPO" --verify-tag --notes-file "$NOTES" --latest=false; fi
done
