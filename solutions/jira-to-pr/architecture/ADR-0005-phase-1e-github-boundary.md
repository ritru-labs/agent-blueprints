# ADR 0005: Scoped GitHub publication and durable draft PR intent

- Status: Implemented in code and tested with a persisted fake GitHub API; live workflow write pending a freshly pinned target base
- Date: 2026-09-27
- Scope: Phase 1E GitHub branch and draft PR boundary; no Jira calls or merge

## Decision

Schema version 4 distinguishes local bare Git publication from GitHub publication. A GitHub publication row is only admitted under an explicit version 2 trusted policy with `external_writes_enabled=true`, `git_branch` and `draft_pr` operations, the fixed repository, an exact target base ref, and the `binnukyadari` actor. Existing version 1 synthetic runs cannot acquire GitHub write intents. The branch remains derived from the run UUID. PostgreSQL keeps candidate, verification, publication, and draft PR links immutable.

The trusted GitHub publisher uses the same isolated Git tree construction as the local proof. Before writing, it reads the authenticated account and repository permissions and requires the target base branch to equal the run's verified base commit. A create-only Git push is preceded by an `OUTCOME_UNKNOWN` publication row. The publisher reads back the exact GitHub ref and commit tree before confirming. If a process dies after the push, the next process reads remote state and confirms the one intended commit. An absent ref after an uncertain push stops for reconciliation.

The draft PR coordinator requires a confirmed GitHub publication of the current `PASS` candidate. It checks the pinned base ref, published head ref, and published Git tree again before planning a bounded, deterministic title and body. PostgreSQL stores SHA-256 hashes of the title and body, not raw model output. It records `OUTCOME_UNKNOWN` before the GitHub create call. On restart, it lists saved pull requests for the exact head/base, requires one open draft by the pinned actor with the exact head SHA and content hashes, and records its number and URL. If no exact saved PR is found, it never blindly resends.

The GitHub API host and repository are fixed in code. The publisher obtains the active GitHub CLI credential only inside the trusted process, verifies the `/user` identity, and gives Git the token through a temporary askpass program. The token is absent from Git command arguments, PostgreSQL, evidence, model input, and the verifier container. GitHub's [pull request API](https://docs.github.com/en/rest/pulls/pulls) accepts `draft`, `head`, and `base`; its [Git ref API](https://docs.github.com/en/rest/git/refs) and [Git commit API](https://docs.github.com/en/rest/git/commits) supply the readback used by this gate.

## Qualification and limits

The control-plane suite runs a separate process after a fake GitHub PR create call and proves that the saved PR is reconciled once. It also rejects base/head/tree/account drift, conflicting saved PRs, missing uncertain outcomes, tampered artifacts, and direct mutation of linked rows. Read-only live GitHub calls confirmed the active `binnukyadari` account, repository push permission, and current branch SHAs. No GitHub workflow branch or PR was created in this qualification step.

The existing saved Phase 1A artifact was verified against an older feature commit. The repository's `main` branch does not contain the sample app; its tip differs from the verifier's pinned base. A live PR needs a target branch choice, a newly pinned policy snapshot, and a fresh Docker verification against that exact base. The draft PR code is not a merge or deployment gate. The original pre-PR independent code-review condition has not been demonstrated and remains a separate acceptance item before treating Phase 1E as fully complete.
