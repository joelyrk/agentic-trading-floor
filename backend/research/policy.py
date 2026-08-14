"""Deterministic source-domain policy, separate from model judgment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .models import ResearchBrief


class ResearchPolicyError(ValueError):
    pass


def _domains(value: str | None) -> frozenset[str]:
    return frozenset(
        item.strip().lower().lstrip(".") for item in (value or "").split(",") if item.strip()
    )


def _matches(domain: str, configured: str) -> bool:
    return domain == configured or domain.endswith(f".{configured}")


@dataclass(frozen=True)
class ResearchPolicy:
    allowed_domains: frozenset[str] = field(default_factory=frozenset)
    denied_domains: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "ResearchPolicy":
        return cls(
            allowed_domains=_domains(os.getenv("RESEARCH_ALLOWED_DOMAINS")),
            denied_domains=_domains(os.getenv("RESEARCH_DENIED_DOMAINS")),
        )

    def validate(self, brief: ResearchBrief) -> None:
        for source in brief.sources:
            domain = (urlsplit(source.canonical_url).hostname or "").lower()
            if any(_matches(domain, denied) for denied in self.denied_domains):
                raise ResearchPolicyError(f"source domain is denied: {domain}")
            if self.allowed_domains and not any(
                _matches(domain, allowed) for allowed in self.allowed_domains
            ):
                raise ResearchPolicyError(f"source domain is not allowed: {domain}")
