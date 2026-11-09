# CI/CD Plan

This document defines the repository CI/CD operating model and required checks.

## Goals

1. Block merges unless quality gates pass.
2. Decouple package publishing from ordinary pushes.
3. Keep expensive integration checks visible and reproducible.

## Workflows

### 1) CI (`.github/workflows/ci.yml`)

Triggers:
- `pull_request`
- `push` to `main`

Required jobs:
- `lint`:
  - `make lint`
- `typecheck`:
  - `make typecheck`
- `arch-contracts`:
  - `make arch`
- `ci-contracts`:
  - `make ci-contracts` (workflow/docs/policy contract tests)
- `blob-scan`:
  - `make scan-blobs` (binary, archive and encoded-payload gate)
- `tests-core`:
  - `make tests PYTEST_XML=pytest-core.xml`
- `tests-full`:
  - `make tests-full PYTEST_XML=pytest-full.xml`
- `package-smoke`:
  - `make package-smoke`

Artifacts uploaded:
- `pytest-core-report`
- `pytest-full-report`
- `dist-packages`

### 2) Integration (`.github/workflows/integration.yml`)

Triggers:
- Nightly schedule (`17 04:00 UTC`)
- Manual (`workflow_dispatch`)

Job:
- `roslyn-integration`
  - Runs `make integration-roslyn`
  - Uses `.github/scripts/roslyn_stub.py` for deterministic CI payloads.

Notes:
- Integration workflow is intentionally separate from required PR checks.
- Failures should be triaged, but do not block normal merges by policy.

### 4) CodeQL (`.github/workflows/codeql.yml`)

Triggers:
- `push` to `main`
- `pull_request`
- Weekly schedule (Mondays at 05:31 UTC)

Job:
- `analyze`
  - Python, `security-extended` query pack, build-mode `none`
  - Results go to the repository's code scanning dashboard

Notes:
- Free for public repositories, using GitHub's own `github/codeql-action`.
- Not a required status check: it reports, it does not gate merges.

## Binary and blob policy

Upstream pull requests are auto-merged without review, so a committed binary
would reach PyPI on the following push. `.github/scripts/scan_blobs.py`
(stdlib only) enforces:

- executables, shared libraries, archives, bytecode and serialised objects
  (`.pkl`, `.joblib`, …) are rejected unconditionally;
- any other non-text file must match a sha256 registered in
  `.github/blob-allowlist.txt`;
- text files above 1 MiB, and quoted base64/hex literals above 512 characters,
  are rejected;
- symbolic links, NUL bytes and invalid UTF-8 are rejected.

The gate runs in three places: `blob-scan` in CI, on every staged file through
`.githooks/pre-commit`, and inside the upstream auto-merge on each pull-request
head before anything is merged. Upstream pull requests may not edit the gate,
its allowlist or the workflows that run it (`--protect-security-files`), so the
gate cannot be disabled by the same mechanism that invokes it.

## Branches

### 3) Publish (`.github/workflows/python-publish.yml`)

Triggers:
- `release.published`
- `push` tag `v*`
- `workflow_dispatch`

Safety gates before publish:
- Validate tag version matches `pyproject.toml` version (for tag pushes)
- Skip publish if version already exists on PyPI
- Run `make scan-blobs` (last line of defence against a committed binary)
- Run `make package-smoke`

## Branch Protection Policy (`main`)

Required status checks:
- `CI / lint`
- `CI / typecheck`
- `CI / arch-contracts`
- `CI / ci-contracts`
- `CI / blob-scan`
- `CI / tests-core`
- `CI / tests-full`
- `CI / package-smoke`

Pull request policy:
- Require PRs before merging
- Require at least 1 approving review
- Dismiss stale approvals on new commits
- Require conversation resolution

Enforcement notes:
- Admin enforcement can be enabled later after workflow stability is proven.

## Local Parity Commands

Use the `Makefile` targets:

- `make ci-fast`: lint + typecheck + import contracts + tests
- `make ci`: `ci-fast` + full tests + package smoke
- `make ci-contracts`: verify CI/workflow/docs contracts
- `make scan-blobs`: run the binary/blob gate over the tracked tree
- `make integration-roslyn`: run Roslyn-path integration parity tests

## Rollout

Phase 1 (immediate):
- Add workflows + local parity targets
- Enable branch protection with required CI checks

Phase 2 (stabilization):
- Monitor integration lane failures and tighten test selection as needed
- Expand mypy coverage gradually by directory

Phase 3 (hardening):
- Enable admin enforcement for branch protection if desired
- Add additional integration lanes (for example, real Roslyn emitter) when infra is available
