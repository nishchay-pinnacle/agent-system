"""Expected source limitations, separate from provider and storage failures."""

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from agent_system.company_intelligence.agent import FetchResult
from agent_system.company_intelligence.page_content import clean_markdown, usable


class SourceLimitation(ValueError):
    """A recorded source outcome the investigator can reason about."""


def unsupported_url(url: str) -> bool:
    return urlsplit(url).path.casefold().endswith(".pdf")


def evidence_problem(fetched: FetchResult) -> str | None:
    if fetched.status_code is not None and fetched.status_code >= 400:
        return f"HTTP {fetched.status_code}; retrieved capture is not citable evidence"
    if fetched.provider_error:
        return f"Provider reported source limitation: {fetched.provider_error}"
    if fetched.content_type and any(
        kind in fetched.content_type.lower()
        for kind in (
            "application/pdf",
            "application/octet-stream",
            "image/",
            "audio/",
            "video/",
        )
    ):
        return f"Unsupported content type: {fetched.content_type}"
    raw = fetched.html or fetched.raw_html
    if raw:
        soup = BeautifulSoup(raw, "html.parser")
        main = soup.find("main") or soup.find(True, attrs={"role": "main"}) or soup
        heading = main.find("h1")
        title = soup.find("title")
        pattern = r"(?:404(?:[ :–-]+(?:page )?not found)?|(?:page )?not found|403(?:[ :–-]+forbidden)?|access denied|forbidden|verify (?:that )?you are human|just a moment)[.! ]*"
        if heading and re.fullmatch(pattern, heading.get_text(" ", strip=True), re.I):
            if (
                not main.find("article")
                or title
                and re.fullmatch(pattern, title.get_text(" ", strip=True), re.I)
            ):
                return "Structural error/access-blocked capture; not usable evidence"
    if not any(value and value.strip() for value in (fetched.markdown, raw)):
        return "Empty capture"
    if raw:
        from agent_system.company_intelligence.html_cleaning import (
            ConversionError,
            clean_html,
        )

        for representation in (fetched.html, fetched.raw_html):
            if representation:
                try:
                    if usable(
                        clean_markdown(
                            clean_html(
                                representation,
                                fetched.final_url or "https://example.invalid/",
                                "capture",
                            ).content
                        )
                    ):
                        return None
                except ConversionError:
                    continue
    if not usable(clean_markdown(fetched.markdown or "")):
        return "No usable cleaned evidence; capture retained as an attempt"
    return None
