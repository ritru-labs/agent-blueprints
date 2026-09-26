"""The controller knows these interfaces, not an Agents API or Jira/GitHub client."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CandidateInput:
    session_id: str
    artifact_id: str
    archive_sha256: str
    archive_bytes: bytes


class CandidateSource(Protocol):
    def read(self) -> CandidateInput:
        ...


class TrustedVerifier(Protocol):
    def verify(self, artifact_path: Path) -> dict:
        ...
