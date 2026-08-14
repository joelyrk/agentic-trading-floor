"""Structured, citation-linked research records with point-in-time guards."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RESEARCHER_PROMPT_VERSION = "researcher-v1"
TRADER_PROMPT_VERSION = "trader-v1"
_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def canonicalize_url(value: str) -> str:
    """Normalize an HTTP(S) URL so tracking variants cannot evade deduplication."""
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("canonical_url must be an absolute HTTP(S) URL")
    host = parts.hostname.lower()
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMETERS
        )
    )
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


class EvidenceStance(StrEnum):
    SUPPORTS = "supports"
    OPPOSES = "opposes"
    MIXED = "mixed"
    CONTEXT = "context"


class SourceRecord(StrictModel):
    source_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    canonical_url: str = Field(min_length=1, max_length=2048)
    publisher: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    published_at: datetime
    retrieved_at: datetime
    supporting_excerpt: str = Field(min_length=1, max_length=500)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    caveats: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("canonical_url", mode="before")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return canonicalize_url(value)

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime, info) -> datetime:
        return _aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_timing_and_hash(self) -> "SourceRecord":
        if self.published_at > self.retrieved_at:
            raise ValueError("published_at cannot be after retrieved_at")
        expected = sha256(self.supporting_excerpt.strip().encode("utf-8")).hexdigest()
        if self.content_hash is not None and self.content_hash != expected:
            raise ValueError("content_hash does not match supporting_excerpt")
        if self.content_hash is None:
            object.__setattr__(self, "content_hash", expected)
        return self


class EvidenceClaim(StrictModel):
    claim_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    claim: str = Field(min_length=1, max_length=1000)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    stance: EvidenceStance
    confidence: Annotated[Decimal, Field(ge=0, le=1)]
    material: bool = True
    caveats: list[str] = Field(default_factory=list, max_length=20)


class ResearchBrief(StrictModel):
    """Concise evidence only. It deliberately has no chain-of-thought field."""

    schema_version: Literal["1.0"] = "1.0"
    research_id: UUID = Field(default_factory=uuid4)
    summary: str = Field(min_length=1, max_length=4000)
    as_of: datetime
    sources: list[SourceRecord] = Field(default_factory=list, max_length=50)
    claims: list[EvidenceClaim] = Field(default_factory=list, max_length=50)
    caveats: list[str] = Field(default_factory=list, max_length=20)
    researcher_prompt_version: str = Field(
        default=RESEARCHER_PROMPT_VERSION, min_length=1, max_length=100
    )

    @field_validator("as_of")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        return _aware(value, "as_of")

    @model_validator(mode="after")
    def validate_evidence_graph(self) -> "ResearchBrief":
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("duplicate source_id")
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("duplicate claim_id")
        known_sources = set(source_ids)
        for claim in self.claims:
            if len(claim.source_ids) != len(set(claim.source_ids)):
                raise ValueError(f"claim {claim.claim_id} has duplicate source IDs")
            if claim.material and not claim.source_ids:
                raise ValueError(f"material claim {claim.claim_id} requires a citation")
            broken = set(claim.source_ids) - known_sources
            if broken:
                raise ValueError(f"claim {claim.claim_id} has unknown source IDs: {sorted(broken)}")
        urls: dict[str, datetime] = {}
        hashes: set[str] = set()
        for source in self.sources:
            prior_time = urls.get(source.canonical_url)
            if prior_time is not None:
                if prior_time != source.published_at:
                    raise ValueError(f"conflicting publication dates for {source.canonical_url}")
                raise ValueError(f"duplicate article: {source.canonical_url}")
            if source.content_hash in hashes:
                raise ValueError("duplicate article content")
            urls[source.canonical_url] = source.published_at
            hashes.add(source.content_hash or "")
            if source.published_at > self.as_of:
                raise ValueError(
                    f"future-dated source {source.source_id} was published after decision cutoff"
                )
        return self
