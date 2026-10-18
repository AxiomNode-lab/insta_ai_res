import os
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from app.core import redis_utils


def _fingerprint_in_child(hash_seed: str) -> str:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = hash_seed
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.core.redis_utils import stable_fingerprint; "
            "print(stable_fingerprint('repeatable synthetic message'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return result.stdout.strip()


def test_fingerprint_is_stable_across_python_hash_seeds():
    first = _fingerprint_in_child("1")
    second = _fingerprint_in_child("987654")

    assert first == second
    assert len(first) == 32
    assert first.isascii()
    assert first.isalnum()


def test_fingerprint_is_keyed_and_changes_with_input():
    first = redis_utils.stable_fingerprint("synthetic-message-one")
    second = redis_utils.stable_fingerprint("synthetic-message-two")

    assert first != "synthetic-message-one"
    assert first != second


@pytest.mark.asyncio
async def test_redis_pattern_cooldown_and_unknown_intent_keys_use_stable_fingerprints(
    monkeypatch,
):
    client = AsyncMock()
    client.incr.side_effect = [1, 2, 1, 2]
    monkeypatch.setattr(
        redis_utils, "get_redis_client", AsyncMock(return_value=client)
    )

    await redis_utils.record_global_reply_pattern(101, "Same configured reply")
    await redis_utils.record_global_reply_pattern(202, "Same configured reply")
    pattern_keys = [call.args[0] for call in client.sadd.await_args_list]
    assert pattern_keys[0] == pattern_keys[1]

    await redis_utils.check_reply_repetition(101, "Same configured reply")
    await redis_utils.check_reply_repetition(101, "Same configured reply")
    repetition_keys = [
        call.args[0] for call in client.incr.await_args_list[:2]
    ]
    assert repetition_keys[0] == repetition_keys[1]

    await redis_utils.record_unknown_intent(101, "What is the price?")
    await redis_utils.record_unknown_intent(101, "What is the price?")
    unknown_keys = [
        call.args[0] for call in client.incr.await_args_list[2:]
    ]
    assert unknown_keys[0] == unknown_keys[1]
