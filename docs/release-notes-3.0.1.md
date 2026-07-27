# Nitro AI Judge CLI 3.0.1

3.0.1 smooths the Play experience without changing manager API v1 or the
minimum compatible CLI version of 3.0.0.

- First-run `naij play` now gives an ordered login, contest-selection, and Play
  checklist when setup is incomplete.
- Manager installation and repair show visible progress and exit cleanly when
  interrupted.
- `naij play cancel [ORG/COMP]` cancels the latest queued or running operation;
  Ctrl-C while waiting prints the exact follow-up status and cancel commands.
- The interactive shell shows one random usage hint when it starts.
- The dashboard performs its first Nitro refresh automatically, keeps top and
  bottom pagination synchronized, and filters active and failed pulls by their
  effective Pulling or Error state.
- The dashboard footer credits Mihnea-Teodor Stoica and previews the Nitro AI
  Judge artwork on hover or keyboard focus.
- `naij play manager update --yes` advances older official manager images to
  3.0.1 while preserving custom and local images unless `--image` is supplied.
- Missing competition images now reuse the two shared, digest-pinned fallback
  images directly. Play no longer creates or deletes per-competition fallback
  tags, so one contest cannot remove fallback images used by another.

Install with `pipx install nitro-ai-judge-cli==3.0.1`. The default manager image
is `ghcr.io/mihneateodorstoica/naij-play-manager:3.0.1`.
