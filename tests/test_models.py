"""Unit tests for turning raw feed messages into CompanyEvent."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from company_recs.models import FEED_TIMEZONE, CompanyEvent, parse_event


def _message(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "company-upserted",
        "registrationNumber": "SYN-000001",
        "updatedAt": "2026-08-14T09:00:00+08:00",
        "data": {
            "companyName": "Kopi Co Sdn. Bhd.",
            "natureOfBusiness": "Operating cafes and coffee shops",
            "state": "Selangor",
            "industry": "Food & Beverage",
        },
    }
    message.update(overrides)
    return message


def _parse(message: Any) -> CompanyEvent:
    return parse_event(json.dumps(message).encode())


def _with_data(**fields: Any) -> dict[str, Any]:
    message = _message()
    message["data"] = {**message["data"], **fields}
    return message


def test_feed_envelope_is_flattened() -> None:
    event = _parse(_message())
    assert event.id == "SYN-000001"
    assert event.name == "Kopi Co Sdn. Bhd."
    assert event.description == "Operating cafes and coffee shops"
    assert event.state == "Selangor"
    assert event.category == "Food & Beverage"
    assert event.is_active is True
    assert event.message_id is None


def test_extra_envelope_fields_are_kept() -> None:
    event = _parse(_message(source="ssm-batch", ingestedBy="partner-api"))
    assert event.model_extra == {"source": "ssm-batch", "ingestedBy": "partner-api"}


@pytest.mark.parametrize("raw_id", ["SYN-000001", " SYN-000001 ", "syn-000001", "SYN- 000001"])
def test_registration_number_is_normalised(raw_id: str) -> None:
    assert _parse(_message(registrationNumber=raw_id)).id == "SYN-000001"


@pytest.mark.parametrize(
    "updated_at",
    [
        "2026-08-14T09:00:00+08:00",
        "2026-08-14T01:00:00Z",
        "2026-08-14T09:00:00",
        int(datetime(2026, 8, 14, 1, tzinfo=UTC).timestamp() * 1000),
    ],
)
def test_every_timestamp_format_means_the_same_instant(updated_at: str | int) -> None:
    event = _parse(_message(updatedAt=updated_at))
    assert event.updatedAt == datetime(2026, 8, 14, 9, tzinfo=FEED_TIMEZONE)
    assert event.updatedAt.tzinfo is not None


def test_missing_industry_is_none() -> None:
    message = _message()
    del message["data"]["industry"]
    assert _parse(message).category is None


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_fields_become_none(blank: str | None) -> None:
    event = _parse(_with_data(natureOfBusiness=blank, industry=blank, state=blank))
    assert (event.description, event.category, event.state) == (None, None, None)


@pytest.mark.parametrize("raw_state", ["selangor", "SELANGOR", " Selangor "])
def test_state_case_is_normalised(raw_state: str) -> None:
    assert _parse(_with_data(state=raw_state)).state == "Selangor"


def test_multi_word_state_is_normalised() -> None:
    assert _parse(_with_data(state="KUALA LUMPUR")).state == "Kuala Lumpur"


def test_struck_off_event_needs_no_data() -> None:
    event = _parse(
        {
            "type": "company-struck-off",
            "registrationNumber": "SYN-000001",
            "updatedAt": "2026-08-14T16:00:00+08:00",
        }
    )
    assert event.is_active is False
    assert event.name is None


def test_flat_form_is_accepted() -> None:
    event = CompanyEvent.model_validate(
        {
            "message_id": "m-1",
            "id": "syn-000001",
            "name": "Kopi Co",
            "category": None,
            "state": None,
            "description": None,
            "updatedAt": "2026-08-14T09:00:00+08:00",
        }
    )
    assert (event.message_id, event.id, event.is_active) == ("m-1", "SYN-000001", True)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b'{"type": "company-upserted", "registrationNumber": "SYN-0', "Unterminated string"),
        (b"null", "valid dictionary"),
        (b"\xff\xfe", "Expecting value"),
        (json.dumps(_message(updatedAt=None)).encode(), "updatedAt"),
        (
            json.dumps({k: v for k, v in _message().items() if k != "updatedAt"}).encode(),
            "updatedAt",
        ),
        (
            json.dumps({k: v for k, v in _message().items() if k != "registrationNumber"}).encode(),
            "id",
        ),
        (json.dumps(_message(registrationNumber=119)).encode(), "must be a string"),
        (json.dumps(_message(registrationNumber="  ")).encode(), "empty"),
        (
            json.dumps(_message(data="companyName=Foo; state=Perak")).encode(),
            "data must be an object",
        ),
        (json.dumps({k: v for k, v in _message().items() if k != "data"}).encode(), "data must be"),
        (json.dumps(_message(type="company-renamed")).encode(), "unknown event type"),
        (json.dumps(_message(updatedAt=True)).encode(), "boolean"),
        (json.dumps(_with_data(companyName=None)).encode(), "company name"),
        (json.dumps(_with_data(companyName="  ")).encode(), "company name"),
    ],
)
def test_unusable_messages_raise_value_error(body: bytes, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_event(body)
