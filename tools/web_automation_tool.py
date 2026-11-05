"""
tools/web_automation_tool.py

Deep Web Extraction & Headless Browser Automation for Alfredo AI OS.
Integrates open-source libraries:
- `crawl4ai` (https://github.com/unclecode/crawl4ai) for LLM-ready Markdown extraction from dynamic webpages
- `playwright` / `browser-use` for interactive headless browser automation (click, fill forms, download, screenshot/PDF)
- Graceful built-in HTML-to-Markdown & table parser fallback so it works out-of-the-box in any local setup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List
import requests
from crewai.tools import tool

logger = logging.getLogger(__name__)

WORKSPACE_DIR = os.path.abspath("workspace")
os.makedirs(WORKSPACE_DIR, exist_ok=True)


def _html_to_clean_markdown(html: str, base_url: str = "", include_links: bool = False) -> str:
    """
    Converts raw HTML into clean, structured Markdown (preserving headings, tables,
    lists, and main article text while stripping navbars, scripts, and styles).
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe", "footer", "nav"]):
            tag.decompose()

        lines: List[str] = []
        title = soup.title.string.strip() if (soup.title and soup.title.string) else ""
        if title:
            lines.append(f"# {title}\n")

        body = soup.find("main") or soup.find("article") or soup.body or soup
        for elem in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "table", "pre"], recursive=True):
            name = elem.name.lower()
            if name in ("h1", "h2", "h3", "h4"):
                lvl = int(name[1])
                txt = elem.get_text(" ", strip=True)
                if txt:
                    lines.append(f"\n{'#' * lvl} {txt}")
            elif name == "p":
                if elem.find_parent("table") or elem.find_parent("li"):
                    continue
                txt = elem.get_text(" ", strip=True)
                if len(txt) > 15:
                    lines.append(txt)
            elif name == "li":
                if elem.find_parent("table"):
                    continue
                txt = elem.get_text(" ", strip=True)
                if txt:
                    lines.append(f"- {txt}")
            elif name == "pre":
                txt = elem.get_text("\n", strip=True)
                if txt:
                    lines.append(f"```\n{txt}\n```")
            elif name == "table":
                rows_md = []
                for r_idx, tr in enumerate(elem.find_all("tr")):
                    cells = [c.get_text(" ", strip=True).replace("|", "/") for c in tr.find_all(["th", "td"])]
                    if cells:
                        rows_md.append("| " + " | ".join(cells) + " |")
                        if r_idx == 0:
                            rows_md.append("| " + " | ".join(["---"] * len(cells)) + " |")
                if rows_md:
                    lines.append("\n" + "\n".join(rows_md) + "\n")

        if include_links:
            links = []
            for a in body.find_all("a", href=True):
                href = a["href"].strip()
                label = a.get_text(" ", strip=True)
                if href.startswith("http") and label:
                    links.append(f"- [{label[:60]}]({href})")
            if links:
                lines.append("\n### Extracted Links\n" + "\n".join(links[:40]))

        text = "\n".join(lines).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text or soup.get_text("\n", strip=True)
    except Exception:
        # Minimal regex fallback if bs4 is absent
        clean = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
        clean = re.sub(r"(?s)<[^>]+>", " ", clean)
        return re.sub(r"\s+", " ", clean).strip()


def _crawl_with_crawl4ai(url: str, extract_links: bool = False) -> str:
    """Attempts extraction via `crawl4ai` AsyncWebCrawler if installed."""
    from crawl4ai import AsyncWebCrawler  # type: ignore

    async def _run():
        async with AsyncWebCrawler(verbose=False) as crawler:
            res = await crawler.arun(url=url)
            md = getattr(res, "markdown", "") or getattr(res, "cleaned_html", "") or ""
            return str(md).strip()

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()
    except Exception as e:
        logger.debug(f"Crawl4AI fallback triggered for {url}: {e}")
        return ""


@tool
def crawl_webpage_markdown(url: str, extract_links: bool = False) -> str:
    """
    Estrae il contenuto completo di una pagina web (anche dinamica) in formato Markdown pulito
    usando Crawl4AI / HTML-to-Markdown strutturato (tabelle, paragrafi, titoli), superando il
    limite dei brevi snippet di DuckDuckGo.
    Args:
        url: URL completo della pagina web (es. 'https://example.com/articolo').
        extract_links: Se True, include anche l'elenco dei link principali trovati nella pagina.
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: Provide a valid URL starting with http:// or https://."

    # 1. Try Crawl4AI first if installed
    try:
        md = _crawl_with_crawl4ai(url, extract_links=extract_links)
        if md and len(md) > 80:
            return md[:18000]
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"Crawl4AI error: {e}")

    # 2. Direct HTTP fetch + structured HTML-to-Markdown (tables, headers, article body)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        md = _html_to_clean_markdown(resp.text, base_url=url, include_links=extract_links)
        if md and len(md.strip()) > 60:
            return md[:18000]
    except Exception as e:
        logger.warning(f"Direct HTTP fetch failed for {url}: {e}")

    # 3. Fallback for JS-rendered SPAs via Jina Reader Markdown gateway
    try:
        jina_resp = requests.get(f"https://r.jina.ai/{url}", headers={"Accept": "text/plain"}, timeout=25)
        if jina_resp.ok and len(jina_resp.text.strip()) > 60:
            return jina_resp.text.strip()[:18000]
    except Exception as e:
        return f"Error crawling '{url}': {e}"

    return f"Error: Could not extract readable content from '{url}'."


@tool
def automate_browser_task(url: str, actions_json: str = "[]") -> str:
    """
    Esegue automazioni interattive nel browser headless (Playwright / Browser-Use):
    naviga su `url`, clicca pulsanti, compila campi, attende caricamenti, salva screenshot/PDF
    nel workspace ed estrae il testo/tabelle risultanti.
    Args:
        url: URL iniziale da aprire.
        actions_json: Lista JSON di azioni opzionali, es.:
            '[{"action": "fill", "selector": "#search", "value": "fatture"},
              {"action": "click", "selector": "button[type=submit]"},
              {"action": "wait", "ms": 1500},
              {"action": "screenshot", "file": "risultato.png"}]'
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://."

    try:
        actions: List[Dict[str, Any]] = json.loads(actions_json or "[]")
        if not isinstance(actions, list):
            actions = []
    except Exception as e:
        return f"Error parsing actions_json: {e}"

    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        # Graceful fallback if Playwright is not installed: extract page via crawl_webpage_markdown
        fallback_md = crawl_webpage_markdown.run(url=url, extract_links=True)
        return (
            "ℹ️ [Playwright non installato — eseguita estrazione profonda Crawl4AI/Markdown]\n\n"
            + str(fallback_md)
        )

    logs: List[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            logs.append(f"Opened {url} (title: {page.title()})")

            for idx, step in enumerate(actions):
                if not isinstance(step, dict):
                    continue
                act = str(step.get("action") or "").lower().strip()
                sel = str(step.get("selector") or "").strip()
                if act == "click" and sel:
                    page.click(sel, timeout=10000)
                    logs.append(f"Step {idx+1}: clicked '{sel}'")
                elif act == "fill" and sel:
                    val = str(step.get("value") or "")
                    page.fill(sel, val, timeout=10000)
                    logs.append(f"Step {idx+1}: filled '{sel}'")
                elif act == "press" and sel:
                    key = str(step.get("key") or "Enter")
                    page.press(sel, key, timeout=10000)
                    logs.append(f"Step {idx+1}: pressed '{key}' on '{sel}'")
                elif act == "wait":
                    ms = int(step.get("ms") or 1000)
                    page.wait_for_timeout(min(ms, 15000))
                    logs.append(f"Step {idx+1}: waited {ms}ms")
                elif act == "screenshot":
                    rel = os.path.basename(str(step.get("file") or "browser_capture.png"))
                    out_path = os.path.join(WORKSPACE_DIR, rel)
                    page.screenshot(path=out_path, full_page=True)
                    logs.append(f"Step {idx+1}: saved screenshot to workspace/{rel}")
                elif act == "pdf":
                    rel = os.path.basename(str(step.get("file") or "browser_page.pdf"))
                    out_path = os.path.join(WORKSPACE_DIR, rel)
                    page.pdf(path=out_path)
                    logs.append(f"Step {idx+1}: saved PDF to workspace/{rel}")

            html_content = page.content()
            browser.close()

        extracted_md = _html_to_clean_markdown(html_content, base_url=url, include_links=False)
        return (
            "### Browser Automation Log\n"
            + "\n".join(f"- {l}" for l in logs)
            + "\n\n### Extracted Page Content (Markdown)\n"
            + extracted_md[:15000]
        )
    except Exception as e:
        return f"Browser automation error on '{url}': {e}"
