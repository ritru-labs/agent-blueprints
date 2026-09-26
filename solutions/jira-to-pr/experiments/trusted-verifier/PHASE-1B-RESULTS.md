# Phase 1B: trusted independent candidate verification

## Decision

**PASS for this local synthetic artifact.** A verifier outside the Agents API sandbox checked the downloaded Phase 1A ZIP against a trusted digest, reconstructed an allowlisted candidate from an immutable Git baseline, and ran verifier-owned checks in a disposable Docker container. The agent's hosted test result was not used as the pass gate. The sanitized facts are in [`evidence/phase-1b-verification.json`](evidence/phase-1b-verification.json); detailed local output remains gitignored.

## Input and reconstruction

- Phase 1A source session: `sess_0bc7de7fdb27ee0d006ab80385ab7c819bbeb057833aa8aa79`; artifact: `artifact_ea210af6c68d411fa36a4c6544b5e928b53b4a181c944983b1`.
- Expected and observed artifact SHA-256: `28ac19606a8ffa3e62833132107110abac1bc33ff621cf1de7926e1d07d30dda`. The ZIP has only `sample/__init__.py`, `sample/app.py`, and `sample/tests/test_app.py` after path, type, duplicate, and size checks.
- Baseline files came from Git commit `7a081367533aa19bcb80d13122e0f3664358a5b8`, not the working tree. Only the three allowed candidate files were overlaid into a new temporary tree.
- Candidate-tree SHA-256: `27ce9688fd58e3eac336c87293a99be1d5410ca93d76744cb6c3dbd9bfbe64cc`. It hashes the canonical sorted mapping of allowed paths to file SHA-256 digests with a `phase1b-tree-v1` domain prefix.

## Independent execution

- Run at 2026-09-26 18:17 UTC with local Python 3.9.6 and Docker Engine 29.8.0. Verifier image pinned to `sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`; `--pull=never` prevented a surprise image fetch.
- Docker used `--network none`, a read-only root filesystem and bind mounts, user `65534:65534`, all capabilities dropped, no new privileges, a 64-process limit, 256 MiB memory limit, 1 CPU, and a 60-second host timeout. The verifier did not mount the repository, `.env.local`, or Docker socket into the candidate container.
- Verifier-owned tests, mounted from outside the ZIP: **5 passed**. They checked `add` cases, preserved `identity`, absence of application token names, inability to write to the candidate path, and lack of outbound networking.
- Candidate-authored tests, run separately after trusted checks: **2 passed**. Both Docker command exit codes were `0`.
- Offline intake and gate tests: **7 passed**. These cover exact-file reconstruction, hash mismatch, traversal, special-file rejection, unchanged source, and rejection of a successful command that discovered zero tests. The live pass gate requires exactly 5 verifier-owned tests, at least 1 candidate test, an `OK` marker, exit code `0`, and no timeout.

## Failure checks

- Appending bytes to the artifact changed its SHA-256; the verifier rejected it before container execution.
- A synthetic wrong `add` implementation returned `0`. Its candidate-authored test passed (exit `0`, 1 test), while the verifier-owned checks failed (exit `1`, 5 tests). The pass gate therefore cannot rely on candidate-authored tests alone.

## Limits and next gate

This proves the local three-file transfer and independent verification path for one synthetic candidate. The checked-in JSON is reviewable metadata, not a signed attestation; the full downloaded ZIP and Docker output are local and gitignored. Docker and the pinned image remain part of the trusted computing base. This phase did not implement Jira, GitHub branch publication, a draft PR, broader repository checks, or production operations. Review the boundary and evidence before expanding the blueprint.
