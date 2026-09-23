"""Deterministic data handoff and validation for scientific literature workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


_FIELD_RE = re.compile(r"^-\s*([^:]+):\s*(.*)$")
_PAPER_RE = re.compile(r"^Paper\s+\d+(?:\s+\(Web Fallback\))?:\s*$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s)>]+", re.IGNORECASE)
_INVALID_OUTPUT_MARKERS = (
    "i will call",
    "assuming the tool",
    "actual final answer will depend",
    "example.com",
    '"name": "search_scientific_literature"',
    "method 1",
    "finding 1",
)


@dataclass(frozen=True)
class PaperRecord:
    title: str
    authors: str
    journal: str
    year: str
    doi: str
    url: str
    abstract: str
    source_index: int


def parse_scientific_search_output(text: str) -> list[PaperRecord]:
    """Parse the stable text contract emitted by search_scientific_literature."""
    papers: list[PaperRecord] = []
    current: dict[str, str] | None = None

    def finish() -> None:
        if not current:
            return
        title = current.get("title", "").strip()
        url = current.get("verified url", "").strip()
        if not title or not url:
            return
        journal_year = current.get("journal/source", "").strip()
        year_match = re.search(r"\(([^()]*)\)\s*$", journal_year)
        year = year_match.group(1).strip() if year_match else "Unknown Year"
        journal = re.sub(r"\s*\([^()]*\)\s*$", "", journal_year).strip()
        papers.append(
            PaperRecord(
                title=title,
                authors=current.get("authors", "Unknown Authors").strip() or "Unknown Authors",
                journal=journal or "Academic Publication",
                year=year,
                doi=current.get("doi", "").strip(),
                url=url,
                abstract=current.get("abstract", "Not stated in the retrieved record").strip()
                or "Not stated in the retrieved record",
                source_index=len(papers),
            )
        )

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if _PAPER_RE.match(line):
            finish()
            current = {}
            continue
        if current is None:
            continue
        match = _FIELD_RE.match(line)
        if match:
            current[match.group(1).strip().lower()] = match.group(2).strip()
    finish()
    return papers


def _tokens(value: str) -> set[str]:
    stop = {
        "and", "the", "for", "with", "from", "using", "model", "modeling",
        "study", "analysis", "application", "applications",
    }
    return {token.lower() for token in _TOKEN_RE.findall(value or "") if token.lower() not in stop}


def rank_papers(papers: Iterable[PaperRecord], topic: str) -> list[PaperRecord]:
    """Rank verified records lexically while preserving Crossref order as a tie-breaker."""
    topic_tokens = _tokens(topic)

    def score(paper: PaperRecord) -> tuple[float, int]:
        title_overlap = len(topic_tokens & _tokens(paper.title))
        abstract_overlap = len(topic_tokens & _tokens(paper.abstract))
        has_doi = bool(paper.doi and paper.doi.upper() != "N/A")
        has_abstract = paper.abstract.lower() != "not stated in the retrieved record"
        value = (title_overlap * 4) + abstract_overlap + int(has_doi) + int(has_abstract)
        return value, -paper.source_index

    return sorted(papers, key=score, reverse=True)


def format_verified_digest(papers: Iterable[PaperRecord], topic: str, limit: int = 5) -> str:
    """Build a safe fallback digest using only retrieved fields."""
    selected = rank_papers(papers, topic)[:limit]
    if len(selected) < limit:
        return (
            f"No complete literature digest could be produced for **{topic}**. "
            f"Only {len(selected)} verified records were retrieved; at least {limit} are required."
        )

    sections = [f"# Scientific literature digest: {topic}"]
    for index, paper in enumerate(selected, 1):
        sections.extend(
            [
                f"### {index}. {paper.title}",
                f"- **Authors / Year**: {paper.authors} ({paper.year})",
                f"- **Journal / Source**: {paper.journal}",
                f"- **DOI**: {paper.doi or 'Not stated in the retrieved record'}",
                f"- **Link / Source**: {paper.url}",
                f"- **What the record says**: {paper.abstract}",
            ]
        )
    sections.extend(
        [
            "### Selection note",
            (
                "These records were selected from the verified search results by lexical relevance "
                f"to **{topic}**, DOI availability, and abstract availability. No facts beyond the "
                "retrieved records were added."
            ),
        ]
    )
    return "\n\n".join(sections)


def curated_digest_is_grounded(candidate: str, papers: Iterable[PaperRecord], limit: int = 5) -> bool:
    """Accept an LLM digest only when its links are grounded in the retrieved records."""
    text = candidate or ""
    lowered = text.lower()
    if any(marker in lowered for marker in _INVALID_OUTPUT_MARKERS):
        return False

    records = list(papers)
    allowed_urls = {paper.url.rstrip(".,") for paper in records}
    output_urls = {url.rstrip(".,") for url in _URL_RE.findall(text)}
    grounded_urls = output_urls & allowed_urls
    foreign_urls = output_urls - allowed_urls
    if foreign_urls or len(grounded_urls) < limit:
        return False

    grounded_titles = sum(1 for paper in records if paper.title.lower() in lowered)
    return grounded_titles >= limit


def ensure_grounded_digest(candidate: str, search_output: str, topic: str, limit: int = 5) -> str:
    """Return the model digest if grounded, otherwise a deterministic verified digest."""
    papers = parse_scientific_search_output(search_output)
    if curated_digest_is_grounded(candidate, papers, limit=limit):
        return candidate
    return format_verified_digest(papers, topic, limit=limit)
