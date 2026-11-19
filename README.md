# Flaky Test Detective

A free, drop-in GitHub Action that tells you when a CI failure isn't your fault.

## Status
Early MVP: roadmap steps 1–2 are implemented, including persisted rolling state
and idempotent artifact ingestion. Tracking issues and fix PRs remain planned.

## How it works
- Detects flakes: a job fails, then passes on a re-run of the same commit SHA.
- Parses JUnit XML (pytest, Jest, Go, Maven/Gradle, RSpec) to pinpoint the test.
- Tracks a rolling flake rate per test in a JSON state file on a dedicated branch.
- Comments on PRs that fail on a known flake: "Matches a known flake (12%), safe to re-run."
- Maintains one tracking issue with a ranked leaderboard of the worst offenders.

## Parallel runners
Each shard uploads JUnit XML plus runner ID, test order, and seed. One serialized
aggregator job merges them and classifies failures (isolation vs. timing/race).
Flagged tests are rerun alone and in original order to confirm shared-state flakes.

## Fix PR guardrails
- Draft-only, never auto-merged; test code only, never production code
- Never skips, retries, or loosens assertions
- Must show before/after evidence from repeated runs
- Diff-size cap, one PR per flaky test, backs off if a PR is closed unmerged

## Roadmap
1. Same-SHA re-run detection + sticky PR comment (implemented)
2. JUnit parsing + per-test flake rate + persisted state (implemented)
3. Tracking issue + leaderboard
4. Isolation classification + agent-drafted fix PRs
5. Validate on 3 public repos with known flakes; publish results

## Rollout
Dogfood on my own repo, then open-source maintainers, then the GitHub Marketplace.

## Quick Start

Drop the action into any workflow that produces JUnit XML:

```yaml
permissions:
  contents: write # Required to persist state
  actions: read
  pull-requests: write # For sticky comments

steps:
  - uses: actions/checkout@v4
  - name: Detect flaky tests
    if: always()
    uses: gpt4omni/flaky-test-detective@main
    with:
      junit-glob: "**/junit*.xml"
      state-file: ".flaky-detective/state.json"
      state-branch: "flaky-detective-state" # Default; "" disables persistence
      fail-on-unexplained: "false" # Set true to gate only unexplained failures
```

The `unexplained-failures` output counts current failures not matching a known
flake. State is stored as `state.json` on the dedicated branch, independently
of the local `state-file` path. Fork pull requests never push state; unavailable
repositories or missing write permissions warn without failing detection.
Concurrent updates are fetched and reapplied with up to three push attempts.

See [`.github/workflows/demo.yml`](.github/workflows/demo.yml) for a full parallel-shard + serialized-aggregator demo workflow.
