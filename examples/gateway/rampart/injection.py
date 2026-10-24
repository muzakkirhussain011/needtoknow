"""Heuristic prompt-injection scoring for user input and for retrieved document chunks.

Indirect injection (instructions hidden inside a document the model reads) is the main risk for a
RAG gateway, so every retrieved chunk is scored and chunks over the threshold are quarantined: they
are not sent to the model and the event is written to the audit log.

This is a deliberately transparent, rule-based first line of defence. It is not a classifier and it
will miss novel phrasings; the `InjectionScorer` interface exists so a model-based detector can be
plugged in. See docs/threat-model.md for what it does and does not cover.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

# (pattern, weight). Weights combine as independent signals: score = 1 - prod(1 - w).
_RULES: tuple[tuple[re.Pattern[str], float, str], ...] = (
    (
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|any|system)\b.{0,20}"
            r"\b(instructions?|rules?|prompts?|guidelines?|directives?)",
            re.I | re.S,
        ),
        0.85,
        "override-instructions",
    ),
    (
        re.compile(
            r"\b(reveal|print|show|repeat|output|leak|dump)\b.{0,40}\b(system prompt|hidden prompt|instructions|"
            r"api[ _-]?keys?|secrets?|credentials?|passwords?)",
            re.I | re.S,
        ),
        0.6,
        "exfiltrate-secrets",
    ),
    (
        re.compile(r"\byou are now\b|\bfrom now on,? you\b|\bact as (?:an?|the)\b|\bpretend (?:to be|you are)\b", re.I),
        0.45,
        "role-hijack",
    ),
    (re.compile(r"\b(DAN|do anything now|developer mode|jailbreak)\b", re.I), 0.6, "jailbreak-keyword"),
    (
        re.compile(r"<\|?(?:im_start|im_end|system|endoftext)\|?>|\[/?INST\]|###\s*(?:system|instruction)", re.I),
        0.6,
        "chat-template-tokens",
    ),
    # Alt text and URL stop at the next "[" (and the URL's first part at its "?"), so each attempt
    # reads only up to the next image: this runs before the policy decides, and the form without
    # those stops took quadratic time on a question full of "![" or "?".
    (re.compile(r"!\[[^\[\]]*\]\(https?://[^)\s?\[]*\?[^)\s\[]*=", re.I), 0.7, "markdown-image-exfiltration"),
    (
        re.compile(
            r"\b(send|post|upload|forward|email)\b.{0,40}\b(to|at)\b.{0,10}(https?://|[\w.+-]+@[\w-]+\.)", re.I | re.S
        ),
        0.5,
        "send-data-out",
    ),
    (re.compile(r"\b(do not|don't|never) (tell|inform|mention to) the user\b", re.I), 0.5, "conceal-from-user"),
    (re.compile(r"[A-Za-z0-9+/]{120,}={0,2}"), 0.25, "long-encoded-blob"),
)


@dataclass(frozen=True)
class InjectionVerdict:
    score: float
    signals: tuple[str, ...]

    def flagged(self, threshold: float) -> bool:
        return self.score >= threshold


class InjectionScorer(Protocol):
    def score(self, text: str) -> InjectionVerdict: ...


class HeuristicInjectionScorer:
    def score(self, text: str) -> InjectionVerdict:
        remaining = 1.0
        signals: list[str] = []
        for pattern, weight, name in _RULES:
            if pattern.search(text):
                remaining *= 1.0 - weight
                signals.append(name)
        return InjectionVerdict(score=round(1.0 - remaining, 4), signals=tuple(signals))
