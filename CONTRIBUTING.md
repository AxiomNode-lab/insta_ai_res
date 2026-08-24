# Contributing to IG Reply Desk

Thank you for helping improve a privacy-conscious Instagram support service.

## Before opening a change

- Use an issue for behavior changes that affect Meta permissions, messaging policy, data retention, or tenant boundaries.
- Never include production credentials, webhook payloads, customer messages, Redis/PostgreSQL exports, or stable customer identifiers.
- Use synthetic account, user, comment, and message IDs in tests and reports.
- Keep outbound behavior user-initiated. Unsolicited broadcasts and policy-bypass features are out of scope.

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
PYTHONPATH=. pytest -q
```

The default tests mock external Meta and storage boundaries. Set `RUN_REDIS_INTEGRATION=1` only against an isolated Redis test database to run the Lua atomicity tests.

## Pull requests

1. Keep one coherent change per PR.
2. Add regression tests for new behavior and failure paths.
3. Update the runbook when operators need a new action.
4. Distinguish local/mock verification from real Meta sandbox verification.
5. Run compilation, tests, dependency checks, and the production image build when relevant.

Security vulnerabilities should follow [SECURITY.md](SECURITY.md), not public issues.
