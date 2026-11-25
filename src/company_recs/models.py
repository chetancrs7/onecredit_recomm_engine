"""The company event model, and how a raw feed message becomes one.

The registry feed nests each change in an envelope:

    {"type": "company-upserted", "registrationNumber": "SYN-000001",
     "updatedAt": "2026-08-14T09:00:00+08:00",
     "data": {"companyName": ..., "natureOfBusiness": ..., "state": ..., "industry": ...}}

`CompanyEvent` is the flat, cleaned-up form the consumer works with. Anything that cannot be
turned into one (invalid JSON, `null`, no registration number, no `updatedAt`, `data` that is not an
object, a numeric registration number) raises `ValueError`, and the consumer dead-letters it.
"""

import json
import string
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

FEED_TIMEZONE: Final = timezone(timedelta(hours=8), "MYT")
EVENT_UPSERTED: Final = "company-upserted"
EVENT_STRUCK_OFF: Final = "company-struck-off"
EVENT_TYPES: Final = frozenset({EVENT_UPSERTED, EVENT_STRUCK_OFF})

_ENVELOPE_KEYS: Final = frozenset({"type", "registrationNumber", "data"})


def normalise_company_id(value: str) -> str:
    """Return a registration number as stored: all whitespace removed, upper case."""
    return "".join(value.split()).upper()


class CompanyEvent(BaseModel):
    """One change to one company, as the consumer applies it.

    `category`, `state` and `description` are `None` when the feed leaves them out, sends `null`
    or sends only whitespace. `is_active` is `False` for a `company-struck-off` event, which carries
    no company data, so `name` is only required for upserts.
    """

    model_config = ConfigDict(extra="allow")

    message_id: str | None = None
    id: str
    name: str | None = None
    category: str | None = None
    state: str | None = None
    description: str | None = None
    updatedAt: datetime
    is_active: bool = True

    @model_validator(mode="before")
    @classmethod
    def _flatten_feed_envelope(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        envelope = cast(dict[str, Any], value)
        if "registrationNumber" not in envelope:
            return envelope
        event_type = envelope.get("type")
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type!r}")
        data = envelope.get("data")
        if event_type == EVENT_UPSERTED and not isinstance(data, dict):
            raise ValueError(f"data must be an object, got {type(data).__name__}")
        fields = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        flat = {key: item for key, item in envelope.items() if key not in _ENVELOPE_KEYS}
        flat.update(
            id=envelope["registrationNumber"],
            name=fields.get("companyName"),
            category=fields.get("industry"),
            state=fields.get("state"),
            description=fields.get("natureOfBusiness"),
            is_active=event_type == EVENT_UPSERTED,
        )
        return flat

    @field_validator("id", mode="before")
    @classmethod
    def _normalise_id(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError(f"registration number must be a string, got {type(value).__name__}")
        normalised = normalise_company_id(value)
        if not normalised:
            raise ValueError("registration number is empty")
        return normalised

    @field_validator("name", "category", "description", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("state", mode="before")
    @classmethod
    def _normalise_state(cls, value: Any) -> Any:
        if isinstance(value, str):
            return string.capwords(value.strip()) or None
        return value

    @field_validator("updatedAt", mode="before")
    @classmethod
    def _parse_epoch_millis(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("updatedAt must be a timestamp, got a boolean")
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value / 1000, UTC)
        return value

    @field_validator("updatedAt", mode="after")
    @classmethod
    def _assume_feed_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=FEED_TIMEZONE)
        return value

    @model_validator(mode="after")
    def _upserts_need_a_name(self) -> CompanyEvent:
        if self.is_active and self.name is None:
            raise ValueError("an upsert must have a company name")
        return self


def parse_event(body: bytes) -> CompanyEvent:
    """Decode one queue message. Raises `ValueError` if it is not a usable company event."""
    return CompanyEvent.model_validate(json.loads(body))
