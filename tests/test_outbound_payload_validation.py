import json

import pytest

from app.services.instagram_service import (
    CrossAccountOutboundPayload,
    InvalidOutboundPayload,
    parse_outbound_queue_payload,
)


def test_valid_outbound_payload_is_normalized_before_delivery():
    payload = {
        "job_id": "synthetic-job",
        "account_id": "42",
        "recipient_id": "synthetic-recipient",
        "recipient_type": "user",
        "text": "Hello",
        "delay": 1,
        "attempt": 0,
    }

    parsed = parse_outbound_queue_payload(json.dumps(payload), expected_account_id=42)

    assert parsed["account_id"] == 42
    assert parsed["delay"] == 1.0
    assert parsed["recipient_id"] == "synthetic-recipient"


@pytest.mark.parametrize(
    "raw_data",
    [
        "{not-json",
        "[]",
        json.dumps({"recipient_id": "synthetic-recipient", "recipient_type": "user", "text": "Hello"}),
        json.dumps({"account_id": 42, "recipient_id": "", "recipient_type": "user", "text": "Hello"}),
        json.dumps({"account_id": 42, "recipient_id": "synthetic-recipient", "recipient_type": "page", "text": "Hello"}),
        json.dumps({"account_id": 42, "recipient_id": "synthetic-recipient", "recipient_type": [], "text": "Hello"}),
        json.dumps({"account_id": 42.5, "recipient_id": "synthetic-recipient", "recipient_type": "user", "text": "Hello"}),
        json.dumps({"account_id": 42, "recipient_id": "synthetic-recipient", "recipient_type": "user", "text": ""}),
        json.dumps({"account_id": 42, "recipient_id": "synthetic-recipient", "recipient_type": "user", "text": "Hello", "delay": "5"}),
        json.dumps({"account_id": 42, "recipient_id": "synthetic-recipient", "recipient_type": "user", "text": "Hello", "delay": -1}),
        json.dumps({"account_id": 42, "recipient_id": "synthetic-recipient", "recipient_type": "user", "text": "Hello", "attempt": -1}),
    ],
)
def test_invalid_outbound_payload_is_rejected_before_delivery(raw_data):
    with pytest.raises(InvalidOutboundPayload):
        parse_outbound_queue_payload(raw_data, expected_account_id=42)


def test_cross_account_payload_is_rejected():
    raw_data = json.dumps(
        {
            "account_id": 43,
            "recipient_id": "synthetic-recipient",
            "recipient_type": "user",
            "text": "Do not send across tenants",
        }
    )

    with pytest.raises(CrossAccountOutboundPayload):
        parse_outbound_queue_payload(raw_data, expected_account_id=42)


def test_non_finite_and_unrepresentably_large_delays_are_rejected():
    for delay in (float("inf"), float("nan"), 10**1000):
        raw_data = json.dumps(
            {
                "account_id": 42,
                "recipient_id": "synthetic-recipient",
                "recipient_type": "user",
                "text": "Hello",
                "delay": delay,
            }
        )
        with pytest.raises(InvalidOutboundPayload):
            parse_outbound_queue_payload(raw_data, expected_account_id=42)
