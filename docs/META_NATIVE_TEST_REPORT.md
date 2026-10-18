# Meta-native feature test report

Date: 2026-10-18

## Automated result

The latest CI run is linked from PR #4; results must be read from the run matching the current branch head, rather than relying on a copied count in this document. CI runs the Python suite, PostgreSQL migration lifecycle, isolated Redis integration tests, production Compose validation, and production-image build.

The tests verify:

- comment automation queues a Meta Private Reply addressed to `comment_id`, not the commenter's ordinary user ID;
- Feed-hosted video comments are recognized while photo Feed comments remain ignored by the configured video-only scope;
- old, malformed, and future supplied comment timestamps are blocked before queueing; missing timestamps are explicitly logged and left to Meta's final enforcement;
- duplicate private replies are not enqueued twice and the outbound queue preserves FIFO ordering;
- ordinary DMs remain addressed to the Instagram-scoped user ID and retain the strict 24-hour check;
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
6. Verify `META_GRAPH_API_VERSION=v26.0`, the login method (Facebook Login vs Instagram Login), host + account-ID-specific send endpoint, required comments/messaging permissions, and actual token against the test app before production rollout.
7. Verify one private reply per comment and the 7-day limit; Live comments need a separate during-broadcast flow and are outside the current video-comment handler scope.

This report demonstrates local contract behavior, not Meta production approval or live delivery.
