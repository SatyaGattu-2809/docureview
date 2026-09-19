# Verification

Verified in this workspace with Python 3.12:

- `pytest -q`: 29 passed.
- `ruff check .`: passed.
- `ruff format --check .`: passed.

Tests include a generated text PDF through the upload endpoint, malformed/encrypted/blank
PDF handling, invoice correction and approval, persistence across app lifespans, concurrent
review conflicts, and mocked Ollama success/error/timeout cases.

Two dependency deprecation warnings were emitted by Starlette's testing support: its httpx
compatibility path and an AnyIO BlockingPortal alias. They did not fail the tests.

Not executed here: live Ollama inference, Docker image build, GitHub-hosted CI, Windows
installation, and production load/security testing. No model extraction accuracy or
throughput benchmark is claimed. Runtime dependency pins reflect the environment tested;
Windows-only colorama is included conditionally but was not installed on Linux.
