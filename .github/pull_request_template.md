## What changed

Describe the user-visible and operational behavior.

## Why

Link the issue or explain the concrete problem.

## Verification

- [ ] `python -m compileall -q app tests`
- [ ] `PYTHONPATH=. pytest -q`
- [ ] `python -m pip check`
- [ ] Docker build completed when deployment files changed

## Safety and privacy

- [ ] No tokens, customer messages, webhook bodies, or stable customer identifiers were added to code, fixtures, screenshots, or logs.
- [ ] Tenant authorization and Meta messaging-window behavior were considered.
- [ ] Failure, retry, duplicate-delivery, and rollback behavior are documented or tested.
- [ ] Any manual Meta sandbox checks still required are listed explicitly.
