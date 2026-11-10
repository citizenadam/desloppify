# Security model

This repository is a fork of [`peteromallet/desloppify`](https://github.com/peteromallet/desloppify)
that automatically merges **every open upstream pull request** into `main`, on a
30-minute schedule, without human review. That design is deliberate: upstream
never merges its own pull requests, so the fork is the integrator.

It also means the usual assumption — "a maintainer read the diff" — does not
hold. This file states what compensates for that, and what does not.

## What the automation enforces

Every upstream pull request is judged against `main` before anything is merged
(`.github/scripts/triage_pending.py`), and the merged tree is re-checked before
it is pushed.

| Gate | File | Rejects |
| --- | --- | --- |
| Blob gate | `.github/scripts/scan_blobs.py` | executables, shared libraries, archives, bytecode, serialised objects (`.pkl`, `.joblib`), any other non-text file without a sha256 in `.github/blob-allowlist.txt`, text above 1 MiB, quoted base64/hex literals above 512 characters, symbolic links |
| Admission policy | `.github/upstream-merge-policy.json` | changes to `.github/`, `Makefile`, `setup.py`, the build backend, `conftest.py`, `MANIFEST.in`, `.gitattributes`, `.gitmodules`, lock files, `requirements*`, import hooks (`sitecustomize.py`, `*.pth`); any added `git+` source, extra index URL, `dependency_links`, or downloaded wheel/sdist URL; changes above 20 files or 1000 lines |
| Secret scanning | GitHub, repository settings | committed credentials (push protection enabled) |
| CodeQL | `.github/workflows/codeql.yml` | reports dangerous execution patterns (`eval`, `exec`, dynamic import, command injection) with the `security-extended` query pack |
| Tests | `.github/workflows/ci.yml` | the merged tree must pass the suite, reproducibility and packaging gates |

Held-back pull requests appear in the auto-merge run summary with the exact
finding. Merging one deliberately means adding it to `allow` in the admission
policy, which is a reviewable commit.

The gates read their own configuration (`blob-allowlist.txt`,
`upstream-merge-policy.json`) from the trusted base ref, so a pull request
cannot approve itself, and changes to the gate files are themselves blocked.

## What it does not do

- **It does not review Python code.** A pull request can still add a backdoor
  to a module, or weaken a test, in plain text. Only CodeQL and the test suite
  look at that code, and neither is a reviewer.
- **It does not sandbox the build.** Publishing uses PyPI trusted publishing
  from a GitHub-hosted runner, only after CI succeeded on that exact commit and
  the commit is an ancestor of `main`. Build provenance is attested for the
  published files (`actions/attest-build-provenance`), so a consumer can verify
  what built a wheel, not whether it is benign.
- **It cannot stop a compromised upstream account** from merging code that is
  plain text and small. The admission limit (20 files / 1000 lines) narrows the
  blast radius; it does not eliminate it.
- **Dependency *names* are not verified.** The policy blocks changed dependency
  *sources* (URLs, indexes, git refs) and all installation machinery, but a
  pull request that adds `requests>=2` to `pyproject.toml` is only held for
  review, not analysed for typosquatting.

## Reporting

If you find a problem in this fork, open an issue at
<https://github.com/citizenadam/desloppify/issues>. Problems that belong to
upstream code rather than the fork should go to
<https://github.com/peteromallet/desloppify/issues>; the fork will pick the fix
up automatically.

If you believe a published version of `desloppify` contains malicious code,
report it on PyPI as well: yanking a release there is what protects users who
already installed it.
