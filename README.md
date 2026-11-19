# Flaky Test Detective

A free, drop-in GitHub Action that tells you when a CI failure isn't your fault.

## Status
Early. Architecture and scope are set; MVP in progress.

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
1. Same-SHA re-run detection + sticky PR comment
2. JUnit parsing + per-test flake rate
3. Tracking issue + leaderboard
4. Isolation classification + agent-drafted fix PRs
5. Validate on 3 public repos with known flakes; publish results

## Rollout
Dogfood on my own repo, then open-source maintainers, then the GitHub Marketplace.
