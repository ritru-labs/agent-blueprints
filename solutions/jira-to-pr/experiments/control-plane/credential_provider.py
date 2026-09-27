"""Trusted credential boundary for GitHub REST and Git transport.

Only this process receives a token. Candidate code and the verifier never do.
An installation-token provider can replace GhCliCredentialProvider without
changing either transport's call sites.
"""

import subprocess
from typing import Protocol


class CredentialProvider(Protocol):
    def token(self) -> str: ...


class GhCliCredentialProvider:
    def token(self) -> str:
        acquired = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                  text=True, timeout=10, check=False)
        if acquired.returncode:
            raise RuntimeError("active GitHub CLI account has no usable token")
        return acquired.stdout.strip()
