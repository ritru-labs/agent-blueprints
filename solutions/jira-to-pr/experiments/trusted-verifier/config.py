"""Trusted Phase 1B inputs, fixed independently of candidate archive contents."""

BASE_COMMIT = "7a081367533aa19bcb80d13122e0f3664358a5b8"
IMAGE_ID = "sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
BASE_PREFIX = "solutions/jira-to-pr/experiments/agents-api-spike/sample-project/"
ALLOWED_FILES = (
    "sample/__init__.py",
    "sample/app.py",
    "sample/tests/test_app.py",
)
MAX_ARCHIVE_BYTES = 1_000_000
MAX_UNCOMPRESSED_BYTES = 2_000_000
