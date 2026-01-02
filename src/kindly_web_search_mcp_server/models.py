from __future__ import annotations

from pydantic import BaseModel, Field


class WebSearchResult(BaseModel):
    title: str = Field(description="Human-readable result title.")
    link: str = Field(description="Canonical URL for the result.")
    snippet: str = Field(description="Search engine snippet/preview text.")
    page_content: str | None = Field(
        default=None,
        description=(
            "LLM-ready Markdown content fetched from `link` (only present when "
            "`return_full_pages=true`)."
        ),
    )


class WebSearchResponse(BaseModel):
    results: list[WebSearchResult]


class GetContentResponse(BaseModel):
    url: str = Field(description="The requested URL.")
    page_content: str = Field(description="LLM-ready Markdown extracted from the URL (best-effort).")
