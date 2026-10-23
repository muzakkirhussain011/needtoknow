"""Detect and mask personal and secret data before it reaches a model.

Each distinct value gets a stable placeholder such as [EMAIL_1], so the model can still refer to
"the same person" without ever seeing the raw value. Authorised callers can have placeholders
restored in the final answer; everyone else keeps the masked text.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    iban = re.sub(r"\s", "", value).upper()
    if not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


@dataclass(frozen=True)
class Detector:
    kind: str
    pattern: re.Pattern[str]
    validate: Callable[[str], bool] | None = None


# Order matters: more specific patterns run first so a card number is not also read as a phone.
# These run on what callers send, before the policy decides, so each runs in linear time: the email
# pattern may only start where a run of address characters starts, never inside one (from every
# position of a long run such as "a.a.a.a..." it was quadratic).
DETECTORS: tuple[Detector, ...] = (
    Detector("SECRET", re.compile(r"\b(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{36})\b")),
    Detector("EMAIL", re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    Detector("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b"), _iban_ok),
    Detector("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), _luhn_ok),
    # Saudi national ID (starts with 1) and Iqama (starts with 2): 10 digits.
    Detector("NATIONAL_ID", re.compile(r"\b[12]\d{9}\b")),
    Detector(
        "PHONE", re.compile(r"(?:\+|00)\d{1,3}[\s\-]?\d{1,4}(?:[\s\-]?\d{2,4}){2,4}\b|\b05\d[\s\-]?\d{3}[\s\-]?\d{4}\b")
    ),
    Detector("IP_ADDRESS", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
)


@dataclass
class RedactionResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original value
    counts: Counter = field(default_factory=Counter)  # kind -> number of values masked


class Redactor:
    """Masks values consistently across several texts in one request (question, retrieved chunks)."""

    def __init__(self, detectors: tuple[Detector, ...] = DETECTORS):
        self._detectors = detectors
        self._value_to_placeholder: dict[str, str] = {}
        self._counters: Counter = Counter()
        self.mapping: dict[str, str] = {}
        self.counts: Counter = Counter()

    def _placeholder(self, kind: str, value: str) -> str:
        key = f"{kind}:{value}"
        if key not in self._value_to_placeholder:
            self._counters[kind] += 1
            placeholder = f"[{kind}_{self._counters[kind]}]"
            self._value_to_placeholder[key] = placeholder
            self.mapping[placeholder] = value
            self.counts[kind] += 1
        return self._value_to_placeholder[key]

    def redact(self, text: str) -> str:
        for det in self._detectors:

            def _sub(match: re.Match[str], det: Detector = det) -> str:
                value = match.group(0)
                if det.validate and not det.validate(value):
                    return value
                return self._placeholder(det.kind, value)

            text = det.pattern.sub(_sub, text)
        return text

    def restore(self, text: str) -> str:
        for placeholder, original in self.mapping.items():
            text = text.replace(placeholder, original)
        return text


def redact(text: str) -> RedactionResult:
    """One-shot helper for a single text."""
    r = Redactor()
    masked = r.redact(text)
    return RedactionResult(text=masked, mapping=dict(r.mapping), counts=Counter(r.counts))
