"""Pydantic schemas for koubo-watch shared data types."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, HttpUrl, field_validator


class Tender(BaseModel):
    """Normalized public tender record fetched from any source."""

    source: str  # "jgrants" | "nedo" | "jst" | "mext"
    external_id: str | None = None
    title: str
    url: str  # canonical URL (validated as HTTP/HTTPS)
    description: str | None = None
    posted_date: date | None = None
    deadline: date | None = None
    # "commissioned" (受注型: 委託・調達・請負) | "subsidy" (助成型) | "unknown"
    # (未確定). filter.pre_label_tender_type() が仮判定し、確定できなければ
    # classifier.classify_tender() の AI 判定で上書きされる（2026-07-20 Fable裁定）。
    tender_type: Literal["commissioned", "subsidy", "unknown"] = "unknown"

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        allowed = {"jgrants", "nedo", "jst", "mext"}
        if v not in allowed:
            raise ValueError(f"source must be one of {allowed}, got {v!r}")
        return v

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://, got {v!r}")
        return v

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title must not be empty")
        return v.strip()
