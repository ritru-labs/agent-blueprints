# Phase 1B trusted independent verifier

This local verifier takes the ZIP downloaded in Phase 1A, checks its SHA-256 against the checked-in [live-run evidence](../agents-api-spike/evidence/phase-1a-live-run.json), rejects unsafe or unexpected archive entries, and reconstructs only three allowed files from the pinned Git baseline plus the candidate ZIP. It computes a deterministic candidate-tree SHA-256 from the sorted file-hash manifest. It then runs verifier-owned requirement checks and candidate tests in separate disposable Docker containers. Neither the agent's hosted test output nor candidate-authored tests alone decide success.

The pinned baseline is commit `7a081367533aa19bcb80d13122e0f3664358a5b8`. The Python image is pinned by immutable image ID in `config.py`; the verifier uses `--pull=never`, so it does not silently fetch a different image. The local image must already be present. Docker access is required.

## Run

```bash
cd solutions/jira-to-pr/experiments/trusted-verifier
python3 -m unittest discover -s test -v
python3 verify_candidate.py
python3 negative_probe.py
```

The default input is the gitignored `../agents-api-spike/.spike-runs/sample-project.zip` from the real Phase 1A run. The verifier does not require `OPENAI_API_KEY`; it makes no Agents API call. A nonzero exit or missing artifact is a failed verification. Detailed bounded local output is written to gitignored `.verifier-runs/last-result.json`. The checked-in `PHASE-1B-RESULTS.md` and `evidence/phase-1b-verification.json` contain only sanitized results.

Each container uses a pinned image, no network, a read-only root filesystem, read-only candidate and trusted-test mounts, a nonroot user, dropped Linux capabilities, no new privileges, and CPU, memory, process, and time limits. A small private `/tmp` tmpfs is provided. The host Docker CLI receives no candidate-supplied flags, and the container receives no repository mount, API key, GitHub or Jira credentials, or Docker socket. The [Docker bind-mount documentation](https://docs.docker.com/engine/storage/bind-mounts/) describes read-only mounts; the [container-run reference](https://docs.docker.com/reference/cli/docker/container/run/) describes the runtime controls used here.

The verifier-owned tests check `add` across positive, zero, and negative cases; preserve `identity`; assert that application token names are absent; and check that writing to the candidate path and outbound networking fail. Candidate-authored tests are run only after the trusted checks pass. This is a narrow synthetic proof, not a general secure executor for arbitrary repositories. Docker isolation and the current three-file allowlist must be reviewed before expanding scope.

The pass gate requires all 5 verifier-owned tests, at least 1 candidate test, an `OK` result, exit code `0`, and no timeout. A successful command that discovers zero tests fails verification.

`negative_probe.py` is an explicit second container exercise: a changed artifact must fail the trusted SHA-256 gate, and a wrong `add` implementation with a candidate-authored passing test must still fail verifier-owned checks. It writes only sanitized metadata to ignored `.verifier-runs/negative-result.json`.
