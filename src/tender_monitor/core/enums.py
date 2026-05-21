from __future__ import annotations

from enum import Enum


class Country(str, Enum):
    KZ = "KZ"
    UZ = "UZ"


class TenderStatus(str, Enum):
    announced = "announced"
    open = "open"
    closed = "closed"
    awarded = "awarded"
    cancelled = "cancelled"
    unknown = "unknown"


class Language(str, Enum):
    ru = "ru"
    kk = "kk"
    uz = "uz"
    en = "en"
    other = "other"


class FeedbackVerdict(str, Enum):
    good_match = "good_match"
    bad_match = "bad_match"
    missed = "missed"


class NotificationChannel(str, Enum):
    telegram = "telegram"
    email = "email"


class NotificationStatus(str, Enum):
    sent = "sent"
    failed = "failed"
    skipped = "skipped"
