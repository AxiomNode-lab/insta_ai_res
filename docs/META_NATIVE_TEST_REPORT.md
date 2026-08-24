# Meta-native feature test report

Date: 2026-08-24

## Automated result

Command:

```bash
PYTHONPATH=. .venv/bin/pytest -q
```

Result: **35 passed**.

The default local run also discovers one Redis integration test and skips it unless `RUN_REDIS_INTEGRATION=1`. CI runs that test against an isolated Redis service before building the production image.

The new tests verify:

- comment automation queues a Meta Private Reply addressed to `comment_id`;
- ordinary DMs remain addressed to the Instagram-scoped user ID;
- message text, quick-reply payloads, and postback title/payload are accepted by the reply engine;
- normal weekday schedules, closed periods, and overnight shifts are evaluated correctly;
- invalid timezone/day/time configuration falls back safely;
- after-hours rate limits run before persistence/Telegram notification, and the cooldown plus queue insert is one atomic Redis operation;
- the Meta Graph API version is explicit and rejects malformed configuration;
- live-chat Telegram alerts resolve only operators linked to the event's tenant and never broadcast customer content to global administrator IDs;
- the existing webhook signature, replay, durable acceptance, rotation, outage, body-size, ops-auth, and redaction tests still pass.

## Manual Meta sandbox checks still required

These cannot be truthfully certified by mocked local tests:

1. Subscribe a Meta test app/account to `messages`, `messaging_postbacks`, and `comments`.
2. Create a Reels/video comment that matches a configured rule and confirm exactly one private reply arrives.
3. Redeliver the identical webhook and confirm no second private reply is sent.
4. Select an Ice Breaker, Persistent Menu item, and Quick Reply and confirm each reaches the configured rule.
5. Send two messages outside configured hours and confirm one notice, two stored inbound messages, and two team notifications.
6. Verify `META_GRAPH_API_VERSION=v26.0` and required permissions against the test app before production rollout.

This report demonstrates local contract behavior, not Meta production approval or live delivery.
