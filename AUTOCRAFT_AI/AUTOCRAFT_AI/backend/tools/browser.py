"""
Real Browser Automation Tools for AUTOCRAFT AI.
Controls actual installed Google Chrome, Microsoft Edge, or Chromium browser using:
1. Playwright (playwright.sync_api) as primary engine when installed, OR
2. Real Chromium/Chrome/Edge WebDriver (selenium.webdriver) when Playwright is not installed.

Strictly prohibits replacing browser navigation with HTTP requests or simulated pages.
Detects CAPTCHA and login/authentication walls and halts immediately rather than bypassing them.
"""

from __future__ import annotations

import re
import os
import ipaddress
import shutil
import threading
import time
import urllib.parse
import logging
from concurrent.futures import ThreadPoolExecutor
from abc import ABC, abstractmethod
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from backend.security import PermissionLevel, SecurityViolationError
from backend.tools.base import (
    PreconditionResult,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    ToolErrorDiagnosis,
    ToolExecutionResult,
    VerificationResult,
)
from backend.tools.registry import ToolRegistry

CAPTCHA_PATTERNS = re.compile(
    r"(g-recaptcha|h-captcha|cf-turnstile|verify you are human|"
    r"please complete the security check|are you a robot|captcha-delivery)",
    re.IGNORECASE,
)
BROWSER_CHANNELS = ("chromium", "chrome", "msedge")
logger = logging.getLogger(__name__)


class CaptchaOrAuthBlockedError(SecurityViolationError):
    """Raised when a page presents a CAPTCHA or authentication challenge that must not be bypassed."""


def _browser_worker_method(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if method.__name__ == "close":
            try:
                return self._run_on_worker(method, *args, **kwargs)
            finally:
                self._shutdown_worker()
        return self._run_on_worker(method, *args, **kwargs)

    return wrapped


class BrowserAutomationSession(ABC):
    """Abstract interface for controlling a real browser instance."""

    def __init__(self) -> None:
        self._worker = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"autocraft-{type(self).__name__}",
        )
        self._worker_thread_id: Optional[int] = None
        self._worker_lock = threading.RLock()
        self._worker_shutdown = False

    def _run_on_worker(self, method, *args, **kwargs):
        if threading.get_ident() == self._worker_thread_id:
            return method(self, *args, **kwargs)

        def invoke():
            self._worker_thread_id = threading.get_ident()
            return method(self, *args, **kwargs)

        with self._worker_lock:
            if self._worker_shutdown:
                raise RuntimeError("Browser session has been closed.")
            future = self._worker.submit(invoke)
        return future.result()

    def _shutdown_worker(self) -> None:
        with self._worker_lock:
            if not self._worker_shutdown:
                self._worker_shutdown = True
                self._worker.shutdown(wait=True)

    @abstractmethod
    def engine_name(self) -> str:
        """Return the active browser engine identifier ('playwright' or 'selenium_chromium')."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if a real browser binary and automation driver are available."""

    @abstractmethod
    def open_url(self, url: str, timeout_ms: int = 15000) -> Dict[str, Any]:
        """Navigate the real browser to url and return current URL, title, and visible text snippet."""

    @abstractmethod
    def youtube_first_result(
        self,
        query: str,
        action: str = "search",
        timeout_ms: int = 20000,
        position: int = 1,
    ) -> Dict[str, Any]:
        """Search YouTube and optionally open/play a selected result."""

    @abstractmethod
    def add_to_wishlist(
        self, site: str, query: str, timeout_ms: int = 20000
    ) -> Dict[str, Any]:
        """Select the highest visibly rated product and verify wishlist confirmation."""

    @abstractmethod
    def wikipedia_research(
        self, query: str, timeout_ms: int = 20000
    ) -> Dict[str, Any]:
        """Extract actual Wikipedia search result titles, article URLs, and excerpts."""

    @abstractmethod
    def search(self, query: str, search_url: Optional[str] = None, timeout_ms: int = 15000) -> Dict[str, Any]:
        """Perform a search using the browser and return results page metadata."""

    @abstractmethod
    def click_element(self, selector: str, timeout_ms: int = 10000) -> Dict[str, Any]:
        """Click an accessible element matching CSS selector or visible text."""

    @abstractmethod
    def scroll_page(self, direction: str = "down", amount: int = 500) -> Dict[str, Any]:
        """Scroll the active page and return scrollY before and after."""

    @abstractmethod
    def read_visible_content(self) -> Dict[str, Any]:
        """Read visible text, title, current URL, and headings from the active browser DOM."""

    @abstractmethod
    def fill_form(self, fields: Dict[str, str], submit: bool = False) -> Dict[str, Any]:
        """Fill form inputs in the active browser DOM and verify their input values."""

    @abstractmethod
    def current_state(self) -> Dict[str, Any]:
        """Return current browser status, active URL, and page title."""

    @abstractmethod
    def close(self) -> None:
        """Close the browser instance cleanly."""


def _check_captcha_in_dom(html_or_text: str, title: str) -> None:
    combined = f"{title}\n{html_or_text}"
    if CAPTCHA_PATTERNS.search(combined):
        raise CaptchaOrAuthBlockedError(
            "CAPTCHA or human-verification protection detected on page. "
            "AUTOCRAFT AI never bypasses CAPTCHA or authentication protections."
        )


def _check_youtube_blocked(page: Any, body_text: str, title: str) -> None:
    page_html = page.content() if hasattr(page, "content") else page.page_source
    current_url = str(getattr(page, "url", getattr(page, "current_url", "")))
    current_host = (urllib.parse.urlsplit(current_url).hostname or "").lower()
    _check_captcha_in_dom(f"{body_text}\n{page_html}", title)
    blocked_text = re.compile(
        r"(before you continue to youtube|sign in to confirm|sign in to continue|"
        r"sign in to prove|sign in to youtube|age-restricted video|confirm your age)",
        re.IGNORECASE,
    )
    if blocked_text.search(f"{title}\n{body_text}") or any(
        current_host == host or current_host.endswith(f".{host}")
        for host in ("consent.youtube.com", "accounts.google.com")
    ):
        raise CaptchaOrAuthBlockedError(
            "YouTube consent, sign-in, or age-verification is required. "
            "Complete it manually; AUTOCRAFT AI will not bypass website controls."
        )
    for selector in ("ytd-consent-bump-v2", "tp-yt-paper-dialog #content"):
        if hasattr(page, "locator"):
            dialog = page.locator(selector).first
            blocked = dialog.count() > 0 and dialog.is_visible()
        else:
            blocked = any(
                dialog.is_displayed()
                for dialog in page.find_elements("css selector", selector)
            )
        if blocked:
            raise CaptchaOrAuthBlockedError(
                "A YouTube consent or sign-in dialog is blocking the requested video. "
                "Complete it manually; AUTOCRAFT AI will not bypass website controls."
            )


def _select_highest_rated_product(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    return max(
        candidates,
        key=lambda item: (
            float(item["rating"]),
            int(item.get("review_count") or 0),
        ),
    )


def _shopping_add_to_wishlist(
    page: Any, site: str, query: str, timeout_ms: int
) -> Dict[str, Any]:
    from urllib.parse import urlsplit

    site_hosts = {"amazon": "amazon.", "flipkart": "flipkart."}
    if site not in site_hosts:
        raise ValueError("Wishlist automation supports Amazon and Flipkart only.")

    current_url = str(getattr(page, "url", getattr(page, "current_url", "")))
    hostname = (urlsplit(current_url).hostname or "").lower()
    if site_hosts[site] not in hostname:
        raise RuntimeError(f"Expected an open {site.capitalize()} page; found '{current_url}'.")

    playwright = hasattr(page, "locator")
    search_selector = (
        "input[type='search'], input[role='searchbox'], input[name='q'], "
        "input[aria-label*='search' i], input[placeholder*='search' i]"
    )
    if playwright:
        search_box = page.locator(search_selector).first
        search_box.wait_for(state="visible", timeout=timeout_ms)
        search_box.fill(query)
        search_box.press("Enter")
    else:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        search_box = WebDriverWait(page, max(1, timeout_ms / 1000)).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, search_selector))
        )
        search_box.clear()
        search_box.send_keys(query)
        search_box.send_keys(Keys.RETURN)

    result_selector = (
        "a[href*='/dp/'], a[href*='/gp/product/']"
        if site == "amazon"
        else "a[href*='/p/']"
    )
    query_parameter = "k" if site == "amazon" else "q"
    query_wait_script = """([parameter, query]) => {
        const currentQuery = new URL(location.href).searchParams.get(parameter);
        return Boolean(currentQuery)
            && currentQuery.toLowerCase() === query.toLowerCase();
    }"""
    result_wait_script = """(selector) => Array.from(
        document.querySelectorAll(selector)
    ).some(link => {
            const rect = link.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        })"""
    result_script = """(selector) => Array.from(document.querySelectorAll(selector))
        .map((link, index) => {
            const card = link.closest('[data-component-type="s-search-result"]')
                || link.closest('[data-id]')
                || link.parentElement?.parentElement;
            if (!card) return null;
            const rect = link.getBoundingClientRect();
            const text = card.innerText || '';
            const ratingNode = card.querySelector(
                '[aria-label*="out of 5" i], [aria-label*="star" i], .a-icon-alt, '
                + '[title*="star" i], img[alt*="star" i], [class*="rating" i], '
                + '[data-rating], .XQDdHH'
            );
            const ratingText = (ratingNode && (
                ratingNode.getAttribute('aria-label') || ratingNode.getAttribute('title')
                || ratingNode.innerText
            )) || text;
            let ratingMatch = ratingText.match(
                /([0-5](?:\\.\\d+)?)\\s*(?:out of 5|\\/\\s*5|★|stars?)/i
            );
            if (!ratingMatch && /^[0-5](?:\\.\\d+)?$/.test(ratingText.trim())
                && (card.querySelector(
                    '[aria-label*="star" i], [title*="star" i], img[alt*="star" i], '
                    + 'svg[aria-label*="star" i]'
                ) || (ratingNode && ratingNode.matches(
                    '[class*="rating" i], [data-rating], .XQDdHH'
                )))) {
                ratingMatch = [ratingText.trim(), ratingText.trim()];
            }
            const reviewMatch = text.match(
                /([\\d,.]+)\\s*([kKmM])?\\s*(?:ratings?|reviews?)/i
            );
            const reviewMultiplier = reviewMatch && reviewMatch[2]
                ? (reviewMatch[2].toLowerCase() === 'k' ? 1000 : 1000000) : 1;
            const title = (link.innerText || link.getAttribute('title')
                || link.querySelector('img')?.alt || '').trim();
            return {
                index,
                href: link.href,
                title,
                rating: ratingMatch ? Number(ratingMatch[1]) : null,
                review_count: reviewMatch
                    ? Math.round(Number(reviewMatch[1].replace(/,/g, '')) * reviewMultiplier)
                    : 0,
                visible: rect.width > 0 && rect.height > 0,
                sponsored: Boolean(card.querySelector(
                    '[aria-label*="sponsored" i], .s-sponsored-label-info-icon'
                ))
            };
        }).filter(item => item && item.visible && item.title && item.href
            && !item.sponsored && item.rating !== null
            && item.rating >= 0 && item.rating <= 5)"""
    auth_probe_script = """() => {
        const visible = element => {
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0
                && getComputedStyle(element).visibility !== 'hidden';
        };
        const loginRoute = /\\/(?:login|signin|sign-in)(?:\\/|$)/i.test(location.pathname);
        const passwordPrompt = Array.from(
            document.querySelectorAll('input[type="password"]')
        ).some(visible);
        const loginDialog = Array.from(document.querySelectorAll(
            '[role="dialog"], [aria-modal="true"]'
        )).some(element => visible(element)
            && /(?:sign|log) in to (?:continue|add)/i.test(element.innerText || ''));
        return loginRoute || passwordPrompt || loginDialog;
    }"""
    if playwright:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            page.wait_for_function(
                query_wait_script,
                arg=[query_parameter, query],
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            html = page.content()
            _check_captcha_in_dom(html, page.title())
            raise LookupError(
                f"{site.capitalize()} did not navigate to search results for '{query}'."
            ) from exc
        html = page.content()
        _check_captcha_in_dom(html, page.title())
        try:
            page.wait_for_function(
                result_wait_script,
                arg=result_selector,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            html = page.content()
            _check_captcha_in_dom(html, page.title())
            raise LookupError(
                f"{site.capitalize()} loaded the search query but showed no visible "
                "product results."
            ) from exc
        candidates = page.evaluate(result_script, result_selector)
    else:
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            WebDriverWait(page, max(1, timeout_ms / 1000)).until(
                lambda driver: driver.execute_script(
                    "return (" + query_wait_script + ")(arguments[0]);",
                    [query_parameter, query],
                )
            )
        except TimeoutException as exc:
            html = page.page_source or ""
            _check_captcha_in_dom(html, page.title or "")
            raise LookupError(
                f"{site.capitalize()} did not navigate to search results for '{query}'."
            ) from exc
        html = page.page_source or ""
        _check_captcha_in_dom(html, page.title or "")
        try:
            WebDriverWait(page, max(1, timeout_ms / 1000)).until(
                lambda driver: driver.execute_script(
                    "return (" + result_wait_script + ")(arguments[0]);",
                    result_selector,
                )
            )
        except TimeoutException as exc:
            html = page.page_source or ""
            _check_captcha_in_dom(html, page.title or "")
            raise LookupError(
                f"{site.capitalize()} loaded the search query but showed no visible "
                "product results."
            ) from exc
        candidates = page.execute_script(
            "return (" + result_script + ")(arguments[0]);", result_selector
        )

    if not candidates:
        raise LookupError(
            "Search results did not expose reliable visible product ratings; the best-rated product "
            "cannot be established, so no product was selected."
        )
    selected = _select_highest_rated_product(candidates)
    if playwright:
        links = page.locator(result_selector)
        links.nth(int(selected["index"])).click(timeout=timeout_ms)
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        product_url = page.url
        body_text = page.locator("body").inner_text(timeout=5000)
        html = page.content()
        password_visible = page.locator("input[type='password']").count() > 0
    else:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        links = page.find_elements(By.CSS_SELECTOR, result_selector)
        links[int(selected["index"])].click()
        WebDriverWait(page, max(1, timeout_ms / 1000)).until(
            lambda driver: driver.execute_script("return document.readyState;")
            in ("interactive", "complete")
        )
        product_url = page.current_url
        body_text = page.find_element(By.TAG_NAME, "body").text
        html = page.page_source
        password_visible = any(
            item.is_displayed()
            for item in page.find_elements(By.CSS_SELECTOR, "input[type='password']")
        )

    _check_captcha_in_dom(html, str(getattr(page, "title", "") or ""))
    product_host = (urlsplit(product_url).hostname or "").lower()
    if site_hosts[site] not in product_host:
        raise RuntimeError(
            f"Selected product navigation left {site.capitalize()} "
            f"(current URL: '{product_url}'). Wishlist action was not attempted."
        )
    current_path = urlsplit(product_url).path.lower()
    if password_visible or any(token in current_path for token in ("login", "signin", "sign-in")):
        raise CaptchaOrAuthBlockedError(
            f"{site.capitalize()} requires manual sign-in before the wishlist action. "
            "Log in yourself and retry; AUTOCRAFT AI will not bypass authentication."
        )
    if not (
        (site == "amazon" and ("/dp/" in current_path or "/gp/product/" in current_path))
        or (site == "flipkart" and "/p/" in current_path)
    ):
        raise RuntimeError(
            f"The selected {site.capitalize()} result did not open a product detail page "
            f"(current URL: '{product_url}'). Wishlist action was not attempted."
        )

    action_pattern = re.compile(
        r"^(?:add to (?:your )?(?:wish ?list|list)|add to wishlist|wishlist)$",
        re.IGNORECASE,
    )
    if playwright:
        action_script = """() => Array.from(document.querySelectorAll(
            'button, [role="button"], a[role="button"]'
        )).map((element, index) => {
            const rect = element.getBoundingClientRect();
            return {
                index,
                label: (element.getAttribute('aria-label') || element.innerText
                    || element.getAttribute('title') || '').trim(),
                visible: rect.width > 0 && rect.height > 0
                    && getComputedStyle(element).visibility !== 'hidden'
            };
        }).filter(item => item.visible)"""
        actions = page.evaluate(action_script)
        match = next(
            (
                item for item in actions
                if action_pattern.fullmatch(str(item["label"]))
                or (
                    site == "amazon"
                    and re.fullmatch(r"add to list", str(item["label"]), re.IGNORECASE)
                )
            ),
            None,
        )
        if match is None:
            raise LookupError(
                f"No identifiable wishlist action was found on the {site.capitalize()} "
                "product page; nothing was added."
            )
        page.locator('button, [role="button"], a[role="button"]').nth(
            int(match["index"])
        ).click(timeout=timeout_ms)
    else:
        from selenium.webdriver.common.by import By

        actions = page.execute_script(
            """return Array.from(document.querySelectorAll(
                'button, [role="button"], a[role="button"]'
            )).map((element, index) => {
                const rect = element.getBoundingClientRect();
                return {
                    index,
                    label: (element.getAttribute('aria-label') || element.innerText
                        || element.getAttribute('title') || '').trim(),
                    visible: rect.width > 0 && rect.height > 0
                        && getComputedStyle(element).visibility !== 'hidden'
                };
            }).filter(item => item.visible);"""
        )
        match = next(
            (
                item for item in actions
                if action_pattern.fullmatch(str(item["label"]))
                or (
                    site == "amazon"
                    and re.fullmatch(r"add to list", str(item["label"]), re.IGNORECASE)
                )
            ),
            None,
        )
        if match is None:
            raise LookupError(
                f"No identifiable wishlist action was found on the {site.capitalize()} "
                "product page; nothing was added."
            )
        page.find_elements(
            By.CSS_SELECTOR, 'button, [role="button"], a[role="button"]'
        )[int(match["index"])].click()

    if site == "amazon":
        option_script = """return Array.from(document.querySelectorAll(
            '[role="menuitem"], [role="option"], button, a'
        )).map((element, index) => {
            const rect = element.getBoundingClientRect();
            return {
                index,
                text: (element.innerText || '').trim(),
                visible: rect.width > 0 && rect.height > 0
            };
        }).filter(item => item.visible && /^wish ?list$/i.test(item.text));"""
        if playwright:
            options = page.locator(
                '[role="menuitem"], [role="option"], button, a'
            ).evaluate_all(
                """items => items.map((element, index) => {
                    const rect = element.getBoundingClientRect();
                    return {
                        index,
                        text: (element.innerText || '').trim(),
                        visible: rect.width > 0 && rect.height > 0
                    };
                }).filter(item => item.visible && /^wish ?list$/i.test(item.text))"""
            )
            if len(options) == 1:
                page.locator(
                    '[role="menuitem"], [role="option"], button, a'
                ).nth(int(options[0]["index"])).click(timeout=timeout_ms)
        else:
            from selenium.webdriver.common.by import By

            options = page.execute_script(option_script)
            if len(options) == 1:
                page.find_elements(
                    By.CSS_SELECTOR, '[role="menuitem"], [role="option"], button, a'
                )[int(options[0]["index"])].click()

    if playwright:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            page.wait_for_function(
                """() => /added to (?:your )?(?:wish ?list|list)/i.test(
                    document.body.innerText
                ) || Array.from(document.querySelectorAll(
                    'button, [role="button"], a[role="button"]'
                )).some(element => /remove from wish ?list|added to wish ?list/i.test(
                    element.getAttribute('aria-label') || element.innerText || ''
                )) || /captcha|verify you are human|security check|are you a robot/i.test(
                    document.body.innerText
                ) || (""" + auth_probe_script + """)()""",
                timeout=min(timeout_ms, 5000),
            )
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                f"{site.capitalize()} did not show verifiable wishlist confirmation."
            ) from exc
        body_text = page.locator("body").inner_text(timeout=5000)
        html = page.content()
    else:
        from selenium.webdriver.support.ui import WebDriverWait

        verified = WebDriverWait(page, min(max(1, timeout_ms / 1000), 5)).until(
            lambda driver: driver.execute_script(
                """return /added to (?:your )?(?:wish ?list|list)/i.test(
                    document.body.innerText
                ) || Array.from(document.querySelectorAll(
                    'button, [role="button"], a[role="button"]'
                )).some(element => /remove from wish ?list|added to wish ?list/i.test(
                    element.getAttribute('aria-label') || element.innerText || ''
                )) || /captcha|verify you are human|security check|are you a robot/i.test(
                    document.body.innerText
                ) || (""" + auth_probe_script + """)();"""
            )
        )
        if not verified:
            raise RuntimeError(
                f"{site.capitalize()} did not show verifiable wishlist confirmation."
            )
        body_text = page.find_element(By.TAG_NAME, "body").text
        html = page.page_source
    _check_captcha_in_dom(html, "")
    auth_required = (
        page.evaluate(auth_probe_script)
        if playwright
        else page.execute_script("return (" + auth_probe_script + ")();")
    )
    if auth_required:
        raise CaptchaOrAuthBlockedError(
            f"{site.capitalize()} requires manual sign-in before the wishlist can be "
            "verified. Log in yourself and retry; AUTOCRAFT AI will not bypass authentication."
        )
    wishlist_state = page.evaluate(action_script) if playwright else page.execute_script(
        """return Array.from(document.querySelectorAll(
            'button, [role="button"], a[role="button"]'
        )).map(element => (
            element.getAttribute('aria-label') || element.innerText
            || element.getAttribute('title') || ''
        ).trim()).filter(label => /remove from (?:your )?wish ?list/i.test(label));"""
    )
    wishlist_state_verified = (
        any(
            re.search(
                r"remove from (?:your )?wish ?list",
                str(item.get("label", "")),
                re.IGNORECASE,
            )
            for item in wishlist_state
        )
        if playwright
        else bool(wishlist_state)
    )
    if not (
        re.search(r"added to (?:your )?(?:wish ?list|list)", body_text, re.IGNORECASE)
        or wishlist_state_verified
    ):
        raise RuntimeError(
            f"{site.capitalize()} did not confirm that the product was added to a wishlist."
        )
    return {
        "site": site,
        "query": query,
        "product_title": str(selected["title"]),
        "rating": float(selected["rating"]),
        "review_count": int(selected.get("review_count") or 0),
        "product_url": product_url,
        "current_url": str(getattr(page, "url", getattr(page, "current_url", ""))),
        "wishlist_added": True,
    }


def _wikipedia_research(page: Any, query: str, timeout_ms: int) -> Dict[str, Any]:
    def is_wikipedia_article_url(url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        article_path = (
            parsed.path[len("/wiki/"):] if parsed.path.startswith("/wiki/") else ""
        )
        namespace = urllib.parse.unquote(article_path).split(":", 1)[0].casefold()
        return (
            parsed.scheme in ("http", "https")
            and (host == "wikipedia.org" or host.endswith(".wikipedia.org"))
            and bool(article_path)
            and namespace
            not in {
                "category",
                "file",
                "help",
                "media",
                "mediawiki",
                "module",
                "portal",
                "special",
                "talk",
                "template",
                "user",
                "wikipedia",
            }
        )

    current_url = str(getattr(page, "url", getattr(page, "current_url", "")))
    hostname = (urllib.parse.urlsplit(current_url).hostname or "").lower()
    if hostname != "wikipedia.org" and not hostname.endswith(".wikipedia.org"):
        raise RuntimeError(
            f"Expected an open Wikipedia page; found '{current_url}'."
        )

    playwright = hasattr(page, "locator")
    search_selector = "#searchInput, input[name='search'], input[type='search']"
    if playwright:
        search_box = page.locator(search_selector).first
        search_box.wait_for(state="visible", timeout=timeout_ms)
        search_box.fill(query)
        search_box.press("Enter")
        page.wait_for_function(
            """() => document.querySelectorAll(
                    'li.mw-search-result h2 a, '
                    + 'li.mw-search-result .mw-search-result-heading a'
                ).length > 0
                || (location.pathname.startsWith('/wiki/')
                    && document.querySelector('#firstHeading'))""",
            timeout=timeout_ms,
        )
    else:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        search_box = WebDriverWait(page, max(1, timeout_ms / 1000)).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, search_selector))
        )
        search_box.clear()
        search_box.send_keys(query)
        search_box.send_keys(Keys.RETURN)
        WebDriverWait(page, max(1, timeout_ms / 1000)).until(
            lambda driver: driver.execute_script(
                """return document.querySelectorAll(
                        'li.mw-search-result h2 a, '
                        + 'li.mw-search-result .mw-search-result-heading a'
                    ).length > 0
                    || (location.pathname.startsWith('/wiki/')
                        && document.querySelector('#firstHeading'));"""
            )
        )

    current_url = str(getattr(page, "url", getattr(page, "current_url", "")))
    extract_script = """() => {
        const entries = Array.from(document.querySelectorAll('li.mw-search-result'))
            .map(item => {
                const link = item.querySelector(
                    'h2 a, .mw-search-result-heading a'
                );
                const rect = item.getBoundingClientRect();
                const excerpt = item.querySelector('.searchresult');
                return link && rect.width > 0 && rect.height > 0 ? {
                    title: (link.innerText || '').trim(),
                    url: link.href,
                    excerpt: excerpt ? (excerpt.innerText || '').trim() : ''
                } : null;
            }).filter(item => item && item.title && item.url);
        if (entries.length) return entries;
        return [];
    }"""
    article_url = current_url if is_wikipedia_article_url(current_url) else ""
    if not article_url:
        if playwright:
            search_results = page.evaluate(extract_script)
        else:
            search_results = page.execute_script("return (" + extract_script + ")();")
        if not isinstance(search_results, list):
            raise LookupError(
                f"Wikipedia returned an invalid search response for '{query}'."
            )

        for result in search_results:
            if not isinstance(result, dict):
                continue
            result_url = str(result.get("url", ""))
            article_title = str(result.get("title", "")).strip()
            if is_wikipedia_article_url(result_url) and article_title:
                article_url = result_url
                break
    if not article_url:
        raise LookupError(
            f"Wikipedia returned no visible article results for '{query}'; "
            "no titles or URLs could be saved."
        )

    if current_url != article_url:
        if playwright:
            page.goto(article_url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_function(
                """() => location.pathname.startsWith('/wiki/')
                    && document.querySelector('#firstHeading')
                    && document.querySelector('#mw-content-text')""",
                timeout=timeout_ms,
            )
        else:
            page.get(article_url)
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.common.by import By

            WebDriverWait(page, max(1, timeout_ms / 1000)).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#firstHeading"))
            )

    article_script = """() => {
        const heading = document.querySelector('#firstHeading');
        const contentRoot = document.querySelector('#mw-content-text');
        if (!heading || !contentRoot || !location.pathname.startsWith('/wiki/')) {
            return null;
        }
        const paragraphs = Array.from(contentRoot.querySelectorAll('p'))
            .map(item => (item.innerText || '').trim())
            .filter(text => text.length > 0)
            .slice(0, 4);
        return {
            title: (heading.innerText || '').trim(),
            url: location.href,
            content: paragraphs.join('\\n\\n').slice(0, 3000)
        };
    }"""
    if playwright:
        article = page.evaluate(article_script)
        current_url = page.url
        page_title = page.title()
    else:
        article = page.execute_script("return (" + article_script + ")();")
        current_url = page.current_url
        page_title = page.title or ""

    if (
        not isinstance(article, dict)
        or not str(article.get("title", "")).strip()
        or not str(article.get("content", "")).strip()
        or str(article.get("url", "")) != current_url
    ):
        raise LookupError(
            f"Wikipedia opened an article for '{query}', but its title or article content "
            "could not be extracted."
        )
    article_content = str(article["content"]).strip()
    if not is_wikipedia_article_url(current_url):
        raise LookupError(
            f"Wikipedia navigation for '{query}' ended at an unexpected URL: "
            f"'{current_url}'."
        )
    result = {
        "title": str(article["title"]).strip(),
        "url": current_url,
        "content": article_content,
        "excerpt": article_content[:450],
    }
    summary_lines = [
        f"Wikipedia article research for: {query}",
        f"Title: {result['title']}",
        f"URL: {result['url']}",
        f"Relevant content:\n{result['content']}",
    ]
    return {
        "engine": "playwright" if playwright else "selenium_chromium",
        "query": query,
        "current_url": current_url,
        "title": page_title,
        "result_count": 1,
        "results": [result],
        "summary": "\n".join(summary_lines),
    }


class PlaywrightBrowserSession(BrowserAutomationSession):
    """Real browser automation backed by Playwright (Chrome, Edge, or Chromium)."""

    def __init__(self, channel: str = "chromium", headless: bool = True) -> None:
        super().__init__()
        self.channel = channel
        self.headless = headless
        self._lock = threading.RLock()
        self._pw = None
        self._browser = None
        self._page = None
        self.availability_error: Optional[str] = None

    def engine_name(self) -> str:
        return "playwright"

    @staticmethod
    def is_installed() -> bool:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return True

    @_browser_worker_method
    def is_available(self) -> bool:
        try:
            self._ensure_page()
        except Exception as exc:
            self.availability_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Playwright initialization failed for browser channel '%s'.",
                self.channel,
            )
            self._close_browser_resources()
            return False
        else:
            self.availability_error = None
            return True

    def _ensure_page(self):
        with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            from playwright.sync_api import sync_playwright

            if self._browser is not None and not self._browser.is_connected():
                self._page = None
                self._browser = None
                if self._pw is not None:
                    self._pw.stop()
                    self._pw = None
            if self._pw is None:
                self._pw = sync_playwright().start()
            if self._browser is None:
                launch_kwargs: Dict[str, Any] = {"headless": self.headless}
                if self.channel in ("chrome", "msedge"):
                    launch_kwargs["channel"] = self.channel
                self._browser = self._pw.chromium.launch(**launch_kwargs)
            if self._page is None or self._page.is_closed():
                self._page = self._browser.new_page()
            return self._page

    @_browser_worker_method
    def open_url(self, url: str, timeout_ms: int = 15000) -> Dict[str, Any]:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        page = self._ensure_page()
        navigation_started = time.monotonic()
        navigation_error = None
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            navigation_error = exc
        except PlaywrightError as exc:
            if "ERR_ABORTED" not in str(exc):
                raise
            navigation_error = exc

        try:
            remaining_ms = max(
                250,
                timeout_ms - int((time.monotonic() - navigation_started) * 1000),
            )
            page.locator("body").wait_for(state="visible", timeout=remaining_ms)
            page.wait_for_function(
                "() => document.readyState === 'interactive' || document.readyState === 'complete'",
                timeout=remaining_ms,
            )
        except PlaywrightTimeoutError:
            if navigation_error is None:
                raise

        title = page.title()
        current_url = page.url
        if not current_url or current_url == "about:blank" or not title.strip():
            if navigation_error is not None:
                raise navigation_error
            raise RuntimeError("Navigation finished without a final URL and page title.")
        body_text = page.locator("body").inner_text(timeout=5000)
        html_content = page.content()
        _check_captcha_in_dom(html_content, title)
        ready_state = page.evaluate("() => document.readyState")
        return {
            "engine": self.engine_name(),
            "browser_channel": self.channel,
            "requested_url": url,
            "current_url": current_url,
            "title": title,
            "ready_state": ready_state,
            "visible_text_preview": body_text[:500].strip(),
            "text_length": len(body_text),
        }

    @_browser_worker_method
    def youtube_first_result(
        self,
        query: str,
        action: str = "search",
        timeout_ms: int = 20000,
        position: int = 1,
    ) -> Dict[str, Any]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        if action not in ("search", "play", "open"):
            raise ValueError("YouTube action must be 'search', 'play', or 'open'.")
        if position not in (1, 2, 3):
            raise ValueError("YouTube result position must be 1, 2, or 3.")
        page = self._ensure_page()
        current_host = (urllib.parse.urlsplit(page.url).hostname or "").lower()
        current_parts = urllib.parse.urlsplit(page.url)
        if not (
            current_host == "youtube.com"
            or current_host.endswith(".youtube.com")
        ):
            if not query.strip():
                raise LookupError(
                    "No active YouTube search results are open. Search for a song or "
                    "video before requesting a result ordinal."
                )
            page.goto(
                "https://www.youtube.com/",
                timeout=timeout_ms,
                wait_until="domcontentloaded",
            )

        body_text = page.locator("body").inner_text(timeout=5000)
        _check_youtube_blocked(page, body_text, page.title())

        if query.strip():
            search_box = page.locator(
                "input#search, ytd-searchbox input[name='search_query'], "
                "input[name='search_query']"
            ).first
            search_box.wait_for(state="visible", timeout=timeout_ms)
            search_box.fill(query)
            search_box.press("Enter")
        else:
            active_query = urllib.parse.parse_qs(current_parts.query).get(
                "search_query", [""]
            )[0]
            if current_parts.path != "/results" or not active_query:
                raise LookupError(
                    "No active YouTube search results are open. Search for a song or "
                    "video before requesting a result ordinal."
                )
            query = active_query

        result_selector = (
            "ytd-video-renderer a#video-title[href*='/watch?v='], "
            "ytd-video-renderer a[href*='/watch?v=']"
        )
        results_required = 1 if action == "search" else position
        try:
            page.wait_for_function(
                """([query, required]) => {
                    const params = new URL(location.href).searchParams;
                    if (location.pathname !== '/results'
                        || params.get('search_query') !== query) return false;
                    const videos = Array.from(document.querySelectorAll(
                        "ytd-video-renderer a#video-title[href*='/watch?v='], "
                        + "ytd-video-renderer a[href*='/watch?v=']"
                    )).filter(item => {
                        const renderer = item.closest('ytd-video-renderer');
                        const rect = item.getBoundingClientRect();
                        return renderer && rect.width > 0 && rect.height > 0
                            && !renderer.querySelector(
                                '#ad-badge, ytd-ad-badge-renderer, '
                                + 'ytd-promoted-sparkles-text-search-renderer'
                            )
                            && item.getAttribute('href') && item.innerText.trim();
                    });
                    const noResults = document.querySelector(
                        'ytd-item-section-renderer #message'
                    );
                    return videos.length >= required
                        || Boolean(noResults
                            && /no results/i.test(noResults.innerText || '')
                            && noResults.getBoundingClientRect().width > 0);
                }""",
                arg=[query, results_required],
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            body_text = page.locator("body").inner_text(timeout=5000)
            _check_youtube_blocked(page, body_text, page.title())
            raise LookupError(
                f"YouTube did not load {results_required} visible video result(s) "
                f"for '{query}'."
            ) from exc
        result_links = page.locator(result_selector)
        results = result_links.evaluate_all(
            """(items) => items.map((item, index) => {
                const renderer = item.closest('ytd-video-renderer');
                const rect = item.getBoundingClientRect();
                return {
                    index,
                    href: item.href,
                    title: (item.getAttribute('title') || item.innerText || '').trim(),
                    visible: rect.width > 0 && rect.height > 0,
                    ad: Boolean(renderer && renderer.querySelector(
                        '#ad-badge, ytd-ad-badge-renderer, '
                        + 'ytd-promoted-sparkles-text-search-renderer'
                    ))
                };
            })"""
        )
        playable_results = [
            item for item in results
            if item.get("href") and "/watch?v=" in item["href"]
            and item.get("title") and item.get("visible") and not item.get("ad")
        ]
        if not playable_results:
            body_text = page.locator("body").inner_text(timeout=5000)
            _check_youtube_blocked(page, body_text, page.title())
            raise LookupError("YouTube search completed, but no playable video result was found.")

        result_titles = [str(item["title"]) for item in playable_results]
        if action == "search":
            return {
                "engine": self.engine_name(),
                "query": query,
                "action": action,
                "result_count": len(playable_results),
                "result_titles": result_titles,
                "selected_position": 0,
                "video_title": "",
                "video_url": page.url,
                "selected_result_url": "",
                "current_url": page.url,
                "title": page.title(),
                "player_ready_state": 0,
                "playback_started": False,
                "current_time": 0.0,
            }
        if len(playable_results) < position:
            raise LookupError(
                f"YouTube returned {len(playable_results)} playable result(s); "
                f"result {position} is unavailable."
            )
        selected_result = playable_results[position - 1]
        selected_video_id = urllib.parse.parse_qs(
            urllib.parse.urlsplit(str(selected_result["href"])).query
        ).get("v", [""])[0]
        result_links.nth(int(selected_result["index"])).click(timeout=timeout_ms)
        try:
            page.wait_for_url(re.compile(r"https?://(?:www\.)?youtube\.com/watch\?"), timeout=timeout_ms)
            video = page.locator("video.html5-main-video, #movie_player video").first
            video.wait_for(state="visible", timeout=timeout_ms)
            page.wait_for_function(
                """() => {
                    const video = document.querySelector('video.html5-main-video, #movie_player video');
                    return video && video.readyState >= 1;
                }""",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            body_text = page.locator("body").inner_text(timeout=5000)
            _check_youtube_blocked(page, body_text, page.title())
            raise LookupError(
                f"YouTube result {position} did not open a loaded video player."
            ) from exc

        opened_video_id = urllib.parse.parse_qs(
            urllib.parse.urlsplit(page.url).query
        ).get("v", [""])[0]
        if not selected_video_id or opened_video_id != selected_video_id:
            raise LookupError(
                f"YouTube opened a different video than visible result {position}."
            )

        body_text = page.locator("body").inner_text(timeout=5000)
        _check_youtube_blocked(page, body_text, page.title())
        player_state = video.evaluate(
            """(element) => ({
                ready_state: element.readyState,
                paused: element.paused,
                current_time: element.currentTime
            })"""
        )
        if action == "play":
            if player_state["paused"]:
                play_button = page.locator("button.ytp-play-button").first
                if not play_button.count() or not play_button.is_visible():
                    raise LookupError("YouTube player loaded but its Play control is unavailable.")
                play_button.click(timeout=timeout_ms)
            try:
                page.wait_for_function(
                    """() => {
                        const video = document.querySelector('video.html5-main-video, #movie_player video');
                        return video && !video.paused && video.currentTime > 0 && video.readyState >= 2;
                    }""",
                    timeout=timeout_ms,
                )
            except PlaywrightTimeoutError as exc:
                body_text = page.locator("body").inner_text(timeout=5000)
                _check_youtube_blocked(page, body_text, page.title())
                raise RuntimeError(
                    "YouTube's player did not start playback; check browser audio, network, or any player prompt."
                ) from exc
            player_state = video.evaluate(
                """(element) => ({
                    ready_state: element.readyState,
                    paused: element.paused,
                    current_time: element.currentTime
                })"""
            )

        return {
            "engine": self.engine_name(),
            "query": query,
            "action": action,
            "result_count": len(playable_results),
            "result_titles": result_titles,
            "selected_position": position,
            "video_title": str(selected_result["title"]),
            "selected_result_url": str(selected_result["href"]),
            "video_url": page.url,
            "current_url": page.url,
            "title": page.title(),
            "player_ready_state": int(player_state["ready_state"]),
            "playback_started": (
                not bool(player_state["paused"])
                and float(player_state["current_time"]) > 0
                and int(player_state["ready_state"]) >= 2
            ),
            "current_time": float(player_state["current_time"]),
        }

    @_browser_worker_method
    def add_to_wishlist(
        self, site: str, query: str, timeout_ms: int = 20000
    ) -> Dict[str, Any]:
        return _shopping_add_to_wishlist(self._ensure_page(), site, query, timeout_ms)

    @_browser_worker_method
    def wikipedia_research(
        self, query: str, timeout_ms: int = 20000
    ) -> Dict[str, Any]:
        return _wikipedia_research(self._ensure_page(), query, timeout_ms)

    @_browser_worker_method
    def search(self, query: str, search_url: Optional[str] = None, timeout_ms: int = 15000) -> Dict[str, Any]:
        page = self._ensure_page()
        if search_url:
            page.goto(search_url, timeout=timeout_ms, wait_until="domcontentloaded")
        elif page.url in ("about:blank", ""):
            encoded = urllib.parse.quote_plus(query)
            page.goto(f"https://www.bing.com/search?q={encoded}", timeout=timeout_ms, wait_until="domcontentloaded")
            title = page.title()
            body_text = page.locator("body").inner_text()
            _check_captcha_in_dom(page.content(), title)
            return {
                "engine": self.engine_name(),
                "query": query,
                "current_url": page.url,
                "title": title,
                "visible_text_preview": body_text[:500].strip(),
            }

        # Find search input on current page
        search_box = page.locator("input[type='search'], input[name='q'], input[type='text']").first
        search_box.fill(query, timeout=5000)
        search_box.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        title = page.title()
        body_text = page.locator("body").inner_text()
        _check_captcha_in_dom(page.content(), title)
        return {
            "engine": self.engine_name(),
            "query": query,
            "current_url": page.url,
            "title": title,
            "visible_text_preview": body_text[:500].strip(),
        }

    @_browser_worker_method
    def click_element(self, selector: str, timeout_ms: int = 10000) -> Dict[str, Any]:
        page = self._ensure_page()
        if re.search(r"\bfirst\s+result\b", selector, re.IGNORECASE):
            result_selector = (
                "a[data-autocraft-search-result], li.b_algo h2 a, "
                "#search a:has(h3), a[data-testid='result-title-a']"
            )
            links = page.locator(result_selector)
            candidates = links.evaluate_all(
                """(items) => items.map((item, index) => {
                    const rect = item.getBoundingClientRect();
                    const parent = item.closest('li, article, [data-testid="result"]');
                    return {
                        index,
                        href: item.href,
                        text: (item.innerText || item.getAttribute('aria-label') || '').trim(),
                        visible: rect.width > 0 && rect.height > 0,
                        ad: Boolean(parent && parent.querySelector('[aria-label*="Ad"], .ad-badge'))
                    };
                })"""
            )
            first_result = next(
                (
                    item for item in candidates
                    if item.get("visible")
                    and item.get("text")
                    and str(item.get("href", "")).startswith(("http://", "https://"))
                    and not item.get("ad")
                ),
                None,
            )
            if first_result is None:
                raise LookupError("No visible, non-ad search result link was found.")
            links.nth(int(first_result["index"])).click(timeout=timeout_ms)
        else:
            loc = page.locator(selector).first
            if loc.count() == 0:
                loc = page.get_by_text(selector).first
            loc.click(timeout=timeout_ms)
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        return {
            "engine": self.engine_name(),
            "selector": selector,
            "clicked": True,
            "current_url": page.url,
            "title": page.title(),
        }

    @_browser_worker_method
    def scroll_page(self, direction: str = "down", amount: int = 500) -> Dict[str, Any]:
        page = self._ensure_page()
        before_y = int(page.evaluate("() => window.scrollY || 0"))
        delta = amount if direction == "down" else (-amount if direction == "up" else 0)
        if direction == "bottom":
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "top":
            page.evaluate("() => window.scrollTo(0, 0)")
        else:
            page.evaluate(f"() => window.scrollBy(0, {int(delta)})")
        after_y = int(page.evaluate("() => window.scrollY || 0"))
        return {
            "engine": self.engine_name(),
            "direction": direction,
            "scroll_y_before": before_y,
            "scroll_y_after": after_y,
            "current_url": page.url,
        }

    @_browser_worker_method
    def read_visible_content(self) -> Dict[str, Any]:
        page = self._ensure_page()
        title = page.title()
        body_text = page.locator("body").inner_text() if page.locator("body").count() else ""
        headings = page.locator("h1, h2, h3").all_inner_texts()
        return {
            "engine": self.engine_name(),
            "current_url": page.url,
            "title": title,
            "visible_text": body_text.strip(),
            "headings": headings[:20],
            "text_length": len(body_text.strip()),
        }

    @_browser_worker_method
    def fill_form(self, fields: Dict[str, str], submit: bool = False) -> Dict[str, Any]:
        page = self._ensure_page()
        verified_fields: Dict[str, str] = {}
        for selector, val in fields.items():
            loc = page.locator(selector).first
            loc.fill(str(val))
            actual_val = loc.input_value()
            verified_fields[selector] = actual_val
        if submit:
            submit_btn = page.locator("button[type='submit'], input[type='submit']").first
            if submit_btn.count() > 0:
                submit_btn.click()
                page.wait_for_load_state("domcontentloaded")
        return {
            "engine": self.engine_name(),
            "filled_count": len(verified_fields),
            "verified_values": verified_fields,
            "submitted": submit,
            "current_url": page.url,
        }

    @_browser_worker_method
    def current_state(self) -> Dict[str, Any]:
        if self._page is None:
            return {"active": False, "engine": self.engine_name()}
        try:
            return {
                "active": not self._page.is_closed(),
                "engine": self.engine_name(),
                "browser_channel": self.channel,
                "current_url": self._page.url,
                "title": self._page.title(),
            }
        except Exception as exc:
            self.availability_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Could not read Playwright browser state.")
            return {
                "active": False,
                "engine": self.engine_name(),
                "error": self.availability_error,
            }

    @_browser_worker_method
    def close(self) -> None:
        with self._lock:
            self._close_browser_resources()

    def _close_browser_resources(self) -> None:
        for resource, close_method in (
            (self._browser, "close"),
            (self._pw, "stop"),
        ):
            if resource is not None:
                try:
                    getattr(resource, close_method)()
                except Exception:
                    logger.exception("Failed to close Playwright resource '%s'.", close_method)
        self._page = None
        self._browser = None
        self._pw = None


class SeleniumChromiumBrowserSession(BrowserAutomationSession):
    """
    Real Chromium/Chrome/Edge browser session using Selenium WebDriver when Playwright is not installed.
    Operates a real browser binary and real DOM (never HTTP requests or simulated pages).
    """

    def __init__(self, channel: str = "chromium", headless: bool = True) -> None:
        super().__init__()
        self.channel = channel
        self.headless = headless
        self._lock = threading.RLock()
        self._driver = None
        self.availability_error: Optional[str] = None

    def engine_name(self) -> str:
        return "selenium_chromium"

    @staticmethod
    def is_installed() -> bool:
        try:
            import selenium.webdriver  # noqa: F401
        except ImportError:
            return False
        return True

    @_browser_worker_method
    def is_available(self) -> bool:
        try:
            self._ensure_driver()
        except Exception as exc:
            self.availability_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Selenium initialization failed for browser channel '%s'.",
                self.channel,
            )
            if self._driver is not None:
                try:
                    self._driver.quit()
                except Exception:
                    logger.exception("Failed to close Selenium after initialization failure.")
                self._driver = None
            return False
        self.availability_error = None
        return True

    @_browser_worker_method
    def _ensure_driver(self):
        with self._lock:
            if self._driver is not None:
                return self._driver
            from selenium import webdriver

            if self.channel == "msedge":
                from selenium.webdriver.edge.options import Options

                create_driver = webdriver.Edge
            else:
                from selenium.webdriver.chrome.options import Options

                create_driver = webdriver.Chrome
            opts = Options()
            if self.headless:
                opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--window-size=1280,900")
            self._driver = create_driver(options=opts)
            self._driver.set_page_load_timeout(15)
            return self._driver

    @_browser_worker_method
    def open_url(self, url: str, timeout_ms: int = 15000) -> Dict[str, Any]:
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._ensure_driver()
        driver.set_page_load_timeout(max(5, int(timeout_ms / 1000)))
        navigation_started = time.monotonic()
        navigation_error = None
        try:
            driver.get(url)
        except TimeoutException as exc:
            navigation_error = exc
        except WebDriverException as exc:
            if "ERR_ABORTED" not in str(exc):
                raise
            navigation_error = exc
        title = driver.title or ""
        current_url = driver.current_url or url
        try:
            remaining_seconds = max(
                0.25,
                timeout_ms / 1000 - (time.monotonic() - navigation_started),
            )
            WebDriverWait(driver, remaining_seconds).until(
                lambda browser: browser.execute_script("return document.readyState;")
                in ("interactive", "complete")
            )
        except TimeoutException:
            if navigation_error is None:
                raise
        title = driver.title or ""
        current_url = driver.current_url or url
        if not title.strip() or current_url in ("", "data:,", "about:blank"):
            if navigation_error is not None:
                raise navigation_error
            raise RuntimeError("Navigation finished without a final URL and page title.")
        page_source = driver.page_source or ""
        _check_captcha_in_dom(page_source, title)
        try:
            body_el = driver.find_element(By.TAG_NAME, "body")
            body_text = body_el.text or ""
        except Exception:
            body_text = ""
        ready_state = driver.execute_script("return document.readyState;")
        return {
            "engine": self.engine_name(),
            "requested_url": url,
            "current_url": current_url,
            "title": title,
            "ready_state": ready_state,
            "visible_text_preview": body_text[:500].strip(),
            "text_length": len(body_text),
        }

    @_browser_worker_method
    def youtube_first_result(
        self,
        query: str,
        action: str = "search",
        timeout_ms: int = 20000,
        position: int = 1,
    ) -> Dict[str, Any]:
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        if action not in ("search", "play", "open"):
            raise ValueError("YouTube action must be 'search', 'play', or 'open'.")
        if position not in (1, 2, 3):
            raise ValueError("YouTube result position must be 1, 2, or 3.")
        driver = self._ensure_driver()
        wait = WebDriverWait(driver, max(1, timeout_ms / 1000))
        current_host = (
            urllib.parse.urlsplit(driver.current_url or "").hostname or ""
        ).lower()
        if not (
            current_host == "youtube.com"
            or current_host.endswith(".youtube.com")
        ):
            if not query.strip():
                raise LookupError(
                    "No active YouTube search results are open. Search for a song or "
                    "video before requesting a result ordinal."
                )
            driver.get("https://www.youtube.com/")
        _check_youtube_blocked(driver, driver.find_element(By.TAG_NAME, "body").text, driver.title or "")
        if query.strip():
            search_box = wait.until(
                EC.visibility_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "input#search, ytd-searchbox input[name='search_query'], "
                        "input[name='search_query']",
                    )
                )
            )
            search_box.clear()
            search_box.send_keys(query)
            search_box.send_keys(Keys.RETURN)
        else:
            current_parts = urllib.parse.urlsplit(driver.current_url or "")
            active_query = urllib.parse.parse_qs(current_parts.query).get(
                "search_query", [""]
            )[0]
            if current_parts.path != "/results" or not active_query:
                raise LookupError(
                    "No active YouTube search results are open. Search for a song or "
                    "video before requesting a result ordinal."
                )
            query = active_query

        result_selector = (
            "ytd-video-renderer a#video-title[href*='/watch?v='], "
            "ytd-video-renderer a[href*='/watch?v=']"
        )
        results_required = 1 if action == "search" else position
        try:
            wait.until(
                lambda browser: (
                    urllib.parse.urlsplit(browser.current_url or "").path == "/results"
                    and urllib.parse.parse_qs(
                        urllib.parse.urlsplit(browser.current_url or "").query
                    ).get("search_query", [""])[0] == query
                    and sum(
                        1
                        for item in browser.find_elements(
                            By.CSS_SELECTOR, result_selector
                        )
                        if item.is_displayed()
                        and item.get_attribute("href")
                        and "/watch?v=" in item.get_attribute("href")
                        and (item.get_attribute("title") or item.text).strip()
                        and not item.find_element(
                            By.XPATH, "ancestor::ytd-video-renderer"
                        ).find_elements(
                            By.CSS_SELECTOR,
                            "#ad-badge, ytd-ad-badge-renderer, "
                            "ytd-promoted-sparkles-text-search-renderer",
                        )
                    ) >= results_required
                    or (
                        urllib.parse.urlsplit(browser.current_url or "").path
                        == "/results"
                        and urllib.parse.parse_qs(
                            urllib.parse.urlsplit(browser.current_url or "").query
                        ).get("search_query", [""])[0] == query
                        and browser.find_elements(
                            By.CSS_SELECTOR, "ytd-item-section-renderer #message"
                        )
                        and any(
                            message.is_displayed()
                            and "no results" in message.text.lower()
                            for message in browser.find_elements(
                                By.CSS_SELECTOR, "ytd-item-section-renderer #message"
                            )
                        )
                    )
                )
            )
        except TimeoutException as exc:
            _check_youtube_blocked(
                driver,
                driver.find_element(By.TAG_NAME, "body").text,
                driver.title or "",
            )
            raise LookupError(
                f"YouTube did not load {results_required} visible video result(s) "
                f"for '{query}'."
            ) from exc
        result_links = driver.find_elements(By.CSS_SELECTOR, result_selector)
        playable_results = []
        for index, link in enumerate(result_links):
            href = link.get_attribute("href") or ""
            title = (link.get_attribute("title") or link.text or "").strip()
            renderer = link.find_element(By.XPATH, "ancestor::ytd-video-renderer")
            if (
                link.is_displayed()
                and href
                and "/watch?v=" in href
                and title
                and not renderer.find_elements(
                    By.CSS_SELECTOR,
                    "#ad-badge, ytd-ad-badge-renderer, "
                    "ytd-promoted-sparkles-text-search-renderer",
                )
            ):
                playable_results.append(
                    {"index": index, "title": title, "href": href}
                )
        if not playable_results:
            _check_youtube_blocked(
                driver, driver.find_element(By.TAG_NAME, "body").text, driver.title or ""
            )
            raise LookupError("YouTube search completed, but no playable video result was found.")

        result_titles = [item["title"] for item in playable_results]
        if action == "search":
            return {
                "engine": self.engine_name(),
                "query": query,
                "action": action,
                "result_count": len(playable_results),
                "result_titles": result_titles,
                "selected_position": 0,
                "video_title": "",
                "video_url": driver.current_url,
                "selected_result_url": "",
                "current_url": driver.current_url,
                "title": driver.title or "",
                "player_ready_state": 0,
                "playback_started": False,
                "current_time": 0.0,
            }
        if len(playable_results) < position:
            raise LookupError(
                f"YouTube returned {len(playable_results)} playable result(s); "
                f"result {position} is unavailable."
            )
        selected_result = playable_results[position - 1]
        selected_video_id = urllib.parse.parse_qs(
            urllib.parse.urlsplit(str(selected_result["href"])).query
        ).get("v", [""])[0]
        result_links[int(selected_result["index"])].click()
        try:
            wait.until(
                lambda browser: (
                    urllib.parse.urlsplit(browser.current_url or "").path == "/watch"
                    and urllib.parse.parse_qs(
                        urllib.parse.urlsplit(browser.current_url or "").query
                    ).get("v", [""])[0] == selected_video_id
                )
            )
            video = wait.until(
                EC.visibility_of_element_located(
                    (By.CSS_SELECTOR, "video.html5-main-video, #movie_player video")
                )
            )
            wait.until(
                lambda browser: browser.execute_script(
                    "return arguments[0].readyState >= 1;", video
                )
            )
        except TimeoutException as exc:
            _check_youtube_blocked(
                driver, driver.find_element(By.TAG_NAME, "body").text, driver.title or ""
            )
            raise LookupError(
                f"YouTube result {position} did not open a loaded video player."
            ) from exc

        opened_video_id = urllib.parse.parse_qs(
            urllib.parse.urlsplit(driver.current_url or "").query
        ).get("v", [""])[0]
        if not selected_video_id or opened_video_id != selected_video_id:
            raise LookupError(
                f"YouTube opened a different video than visible result {position}."
            )

        _check_youtube_blocked(
            driver, driver.find_element(By.TAG_NAME, "body").text, driver.title or ""
        )
        player_state = driver.execute_script(
            """return {
                ready_state: arguments[0].readyState,
                paused: arguments[0].paused,
                current_time: arguments[0].currentTime
            };""",
            video,
        )
        if action == "play":
            if player_state["paused"]:
                play_controls = driver.find_elements(By.CSS_SELECTOR, "button.ytp-play-button")
                if not play_controls or not play_controls[0].is_displayed():
                    raise LookupError("YouTube player loaded but its Play control is unavailable.")
                play_controls[0].click()
            try:
                wait.until(
                    lambda browser: browser.execute_script(
                        """const video = document.querySelector('video.html5-main-video, #movie_player video');
                        return video && !video.paused && video.currentTime > 0 && video.readyState >= 2;"""
                    )
                )
            except TimeoutException as exc:
                _check_youtube_blocked(
                    driver, driver.find_element(By.TAG_NAME, "body").text, driver.title or ""
                )
                raise RuntimeError(
                    "YouTube's player did not start playback; check browser audio, network, or any player prompt."
                ) from exc
            player_state = driver.execute_script(
                """return {
                    ready_state: arguments[0].readyState,
                    paused: arguments[0].paused,
                    current_time: arguments[0].currentTime
                };""",
                video,
            )

        return {
            "engine": self.engine_name(),
            "query": query,
            "action": action,
            "result_count": len(playable_results),
            "result_titles": result_titles,
            "selected_position": position,
            "video_title": selected_result["title"],
            "selected_result_url": selected_result["href"],
            "video_url": driver.current_url,
            "current_url": driver.current_url,
            "title": driver.title or "",
            "player_ready_state": int(player_state["ready_state"]),
            "playback_started": (
                not bool(player_state["paused"])
                and float(player_state["current_time"]) > 0
                and int(player_state["ready_state"]) >= 2
            ),
            "current_time": float(player_state["current_time"]),
        }

    @_browser_worker_method
    def add_to_wishlist(
        self, site: str, query: str, timeout_ms: int = 20000
    ) -> Dict[str, Any]:
        return _shopping_add_to_wishlist(self._ensure_driver(), site, query, timeout_ms)

    @_browser_worker_method
    def wikipedia_research(
        self, query: str, timeout_ms: int = 20000
    ) -> Dict[str, Any]:
        return _wikipedia_research(self._ensure_driver(), query, timeout_ms)

    @_browser_worker_method
    def search(self, query: str, search_url: Optional[str] = None, timeout_ms: int = 15000) -> Dict[str, Any]:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        driver = self._ensure_driver()
        if search_url:
            driver.get(search_url)
        elif not driver.current_url or driver.current_url in ("data:,", "about:blank"):
            encoded = urllib.parse.quote_plus(query)
            driver.get(f"https://www.bing.com/search?q={encoded}")
            title = driver.title or ""
            _check_captcha_in_dom(driver.page_source or "", title)
            body_text = driver.find_element(By.TAG_NAME, "body").text
            return {
                "engine": self.engine_name(),
                "query": query,
                "current_url": driver.current_url,
                "title": title,
                "visible_text_preview": body_text[:500].strip(),
            }

        inputs = driver.find_elements(
            By.CSS_SELECTOR, "input[type='search'], input[name='q'], input[type='text']"
        )
        if not inputs:
            raise LookupError("No accessible search input element found on current page.")
        box = inputs[0]
        box.clear()
        box.send_keys(query)
        box.send_keys(Keys.RETURN)
        time.sleep(0.3)
        title = driver.title or ""
        _check_captcha_in_dom(driver.page_source or "", title)
        body_text = driver.find_element(By.TAG_NAME, "body").text
        return {
            "engine": self.engine_name(),
            "query": query,
            "current_url": driver.current_url,
            "title": title,
            "visible_text_preview": body_text[:500].strip(),
        }

    @_browser_worker_method
    def click_element(self, selector: str, timeout_ms: int = 10000) -> Dict[str, Any]:
        from selenium.webdriver.common.by import By

        driver = self._ensure_driver()
        if re.search(r"\bfirst\s+result\b", selector, re.IGNORECASE):
            result_selector = (
                "a[data-autocraft-search-result], li.b_algo h2 a, "
                "#search a:has(h3), a[data-testid='result-title-a']"
            )
            target = next(
                (
                    item
                    for item in driver.find_elements(By.CSS_SELECTOR, result_selector)
                    if item.is_displayed()
                    and (item.text or item.get_attribute("aria-label") or "").strip()
                    and (item.get_attribute("href") or "").startswith(("http://", "https://"))
                    and not item.find_elements(
                        By.CSS_SELECTOR, "[aria-label*='Ad'], .ad-badge"
                    )
                ),
                None,
            )
        else:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if not elements:
                elements = driver.find_elements(
                    By.XPATH,
                    f"//*[contains(normalize-space(text()), '{selector}')]",
                )
            target = elements[0] if elements else None
        if target is None:
            raise LookupError(f"Visible element or result matching '{selector}' was not found.")
        if not target.is_displayed():
            raise LookupError(f"Element '{selector}' is not visible on the page.")
        target.click()
        time.sleep(0.2)
        return {
            "engine": self.engine_name(),
            "selector": selector,
            "clicked": True,
            "current_url": driver.current_url,
            "title": driver.title or "",
        }

    @_browser_worker_method
    def scroll_page(self, direction: str = "down", amount: int = 500) -> Dict[str, Any]:
        driver = self._ensure_driver()
        before_y = int(driver.execute_script("return window.scrollY || window.pageYOffset || 0;") or 0)
        if direction == "bottom":
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        elif direction == "top":
            driver.execute_script("window.scrollTo(0, 0);")
        else:
            delta = amount if direction == "down" else -amount
            driver.execute_script(f"window.scrollBy(0, {int(delta)});")
        time.sleep(0.1)
        after_y = int(driver.execute_script("return window.scrollY || window.pageYOffset || 0;") or 0)
        return {
            "engine": self.engine_name(),
            "direction": direction,
            "scroll_y_before": before_y,
            "scroll_y_after": after_y,
            "current_url": driver.current_url,
        }

    @_browser_worker_method
    def read_visible_content(self) -> Dict[str, Any]:
        from selenium.webdriver.common.by import By

        driver = self._ensure_driver()
        title = driver.title or ""
        _check_captcha_in_dom(driver.page_source or "", title)
        body_text = driver.find_element(By.TAG_NAME, "body").text.strip()
        heading_els = driver.find_elements(By.CSS_SELECTOR, "h1, h2, h3")
        headings = [h.text.strip() for h in heading_els if h.text.strip()]
        return {
            "engine": self.engine_name(),
            "current_url": driver.current_url,
            "title": title,
            "visible_text": body_text,
            "headings": headings[:20],
            "text_length": len(body_text),
        }

    @_browser_worker_method
    def fill_form(self, fields: Dict[str, str], submit: bool = False) -> Dict[str, Any]:
        from selenium.webdriver.common.by import By

        driver = self._ensure_driver()
        verified_fields: Dict[str, str] = {}
        for selector, val in fields.items():
            els = driver.find_elements(By.CSS_SELECTOR, selector)
            if not els:
                els = driver.find_elements(By.NAME, selector)
            if not els:
                raise LookupError(f"Form field '{selector}' not found in DOM.")
            field_el = els[0]
            field_el.clear()
            field_el.send_keys(str(val))
            actual_val = field_el.get_attribute("value") or ""
            verified_fields[selector] = actual_val

        if submit:
            submits = driver.find_elements(
                By.CSS_SELECTOR, "button[type='submit'], input[type='submit']"
            )
            if submits:
                submits[0].click()
                time.sleep(0.2)

        return {
            "engine": self.engine_name(),
            "filled_count": len(verified_fields),
            "verified_values": verified_fields,
            "submitted": submit,
            "current_url": driver.current_url,
        }

    @_browser_worker_method
    def current_state(self) -> Dict[str, Any]:
        if self._driver is None:
            return {"active": False, "engine": self.engine_name()}
        try:
            return {
                "active": True,
                "engine": self.engine_name(),
                "current_url": self._driver.current_url,
                "title": self._driver.title,
            }
        except Exception as exc:
            self.availability_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Could not read Selenium browser state.")
            return {
                "active": False,
                "engine": self.engine_name(),
                "error": self.availability_error,
            }

    @_browser_worker_method
    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                try:
                    self._driver.quit()
                except Exception:
                    logger.exception("Failed to close Selenium browser.")
                self._driver = None


_BROWSER_SESSIONS: Dict[Tuple[str, bool], BrowserAutomationSession] = {}
_BROWSER_LOCK = threading.RLock()


def get_browser_session(channel: str = "chromium", headless: bool = True) -> BrowserAutomationSession:
    """Return or initialize the active real browser automation session."""
    if channel == "auto":
        channel = "chromium"
    with _BROWSER_LOCK:
        key = (channel, headless)
        session = _BROWSER_SESSIONS.get(key)
        if session is not None:
            return session

        if PlaywrightBrowserSession.is_installed():
            session = PlaywrightBrowserSession(channel=channel, headless=headless)
        else:
            session = SeleniumChromiumBrowserSession(channel=channel, headless=headless)
        _BROWSER_SESSIONS[key] = session
        return session


def set_browser_session(session: Optional[BrowserAutomationSession]) -> None:
    """Override the active browser session (for testing or reset)."""
    with _BROWSER_LOCK:
        for active_session in _BROWSER_SESSIONS.values():
            if active_session is not session:
                active_session.close()
        _BROWSER_SESSIONS.clear()
        if session is not None:
            _BROWSER_SESSIONS[("chromium", True)] = session


def _session_for(params: Dict[str, Any], ctx: ToolContext) -> BrowserAutomationSession:
    channel = str(params.get("browser", ctx.config.browser_channel))
    if channel == "auto":
        channel = "chromium"
    if channel not in BROWSER_CHANNELS:
        raise ValueError(
            f"Unsupported browser '{channel}'. Choose Chromium, Chrome, or Microsoft Edge."
        )
    return get_browser_session(channel, ctx.config.browser_headless)


def close_browser_session() -> None:
    set_browser_session(None)


def _validate_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned or re.search(r"[\s\x00-\x1f]", cleaned):
        raise ValueError("URL cannot be empty or contain whitespace/control characters.")
    if re.match(r"^(?:http|https):(?://)?", cleaned, re.IGNORECASE) and not re.match(
        r"^https?://", cleaned, re.IGNORECASE
    ):
        raise ValueError("HTTP URLs must include '://' after the scheme.")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", cleaned) and not re.match(
        r"^https?://", cleaned, re.IGNORECASE
    ):
        raise ValueError("Only http:// and https:// URLs are supported.")
    if not re.match(r"^https?://", cleaned, re.IGNORECASE):
        cleaned = "https://" + cleaned
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Invalid URL '{url}'. Must be a valid http:// or https:// address.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing embedded credentials are not supported.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid URL port in '{url}'.") from exc
    hostname = parsed.hostname.rstrip(".")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(f"Invalid URL host in '{url}'.") from exc
        labels = ascii_hostname.split(".")
        if len(ascii_hostname) > 253 or any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            for label in labels
        ):
            raise ValueError(f"Invalid URL host in '{url}'.")
        normalized_host = ascii_hostname.lower()
    else:
        normalized_host = f"[{hostname.lower()}]" if ":" in hostname else hostname.lower()
    authority = normalized_host + (f":{port}" if port is not None else "")
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), authority, parsed.path, parsed.query, parsed.fragment)
    )


def _same_site_family(requested_url: str, current_url: str) -> bool:
    requested_host = (urllib.parse.urlsplit(requested_url).hostname or "").lower().rstrip(".")
    current_host = (urllib.parse.urlsplit(current_url).hostname or "").lower().rstrip(".")
    return bool(
        requested_host
        and current_host
        and (
            requested_host == current_host
            or requested_host.endswith(f".{current_host}")
            or current_host.endswith(f".{requested_host}")
        )
    )


def _browser_precondition(params: Dict[str, Any], ctx: ToolContext) -> PreconditionResult:
    try:
        session = _session_for(params, ctx)
    except ValueError as exc:
        return PreconditionResult(
            satisfied=False,
            reason=str(exc),
            details={"error_code": "UNSUPPORTED_BROWSER"},
        )
    if not session.is_available():
        return PreconditionResult(
            satisfied=False,
            reason=(
                getattr(session, "availability_error", None)
                or "No real browser engine (Playwright or Chromium WebDriver) is available on this host."
            ),
            details={"error_code": "BROWSER_ENGINE_UNAVAILABLE"},
        )
    if "url" in params and params["url"]:
        try:
            _validate_url(params["url"])
        except ValueError as exc:
            return PreconditionResult(
                satisfied=False,
                reason=str(exc),
                details={"error_code": "INVALID_URL"},
            )
    return PreconditionResult(satisfied=True, details={"engine": session.engine_name()})


def _browser_error_handler(exc: Exception, params: Dict[str, Any]) -> ToolErrorDiagnosis:
    if isinstance(exc, CaptchaOrAuthBlockedError):
        return ToolErrorDiagnosis(
            error_code="SECURITY_CAPTCHA_OR_AUTH_BLOCKED",
            category="SECURITY",
            message=str(exc),
            retryable=False,
            recovery_suggestion="Complete the CAPTCHA or authentication manually; the agent never bypasses security challenges.",
        )
    if isinstance(exc, LookupError):
        return ToolErrorDiagnosis(
            error_code="BROWSER_ELEMENT_NOT_FOUND",
            category="BROWSER_DOM",
            message=str(exc),
            retryable=True,
            recovery_suggestion="Verify the page finished loading and the selector matches a visible element.",
        )
    msg = str(exc)
    if "ERR_NAME_NOT_RESOLVED" in msg or "ERR_INTERNET_DISCONNECTED" in msg or "net::" in msg:
        return ToolErrorDiagnosis(
            error_code="BROWSER_NETWORK_UNREACHABLE",
            category="NETWORK",
            message=f"Browser could not reach target host: {msg}",
            retryable=True,
            recovery_suggestion="Check internet connectivity or verify the target URL.",
        )
    return ToolErrorDiagnosis(
        error_code="BROWSER_AUTOMATION_ERROR",
        category="BROWSER",
        message=f"{type(exc).__name__}: {exc}",
        retryable=True,
        recovery_suggestion="Check browser state and target selector.",
    )


# --- Browser Tool Functions ---

def _exec_open_url(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    session = _session_for(params, ctx)
    normalized_url = _validate_url(params["url"])
    out = session.open_url(normalized_url, timeout_ms=ctx.config.browser_timeout_ms)
    return ToolExecutionResult(success=True, output=out)


def _ver_open_url(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    curr_url = str(res.output.get("current_url", ""))
    requested_url = _validate_url(str(params.get("url", "")))
    ready = str(res.output.get("ready_state", ""))
    final_url = urllib.parse.urlsplit(curr_url)
    expected_browser = str(params.get("browser", ctx.config.browser_channel))
    if expected_browser == "auto":
        expected_browser = "chromium"
    actual_browser = str(res.output.get("browser_channel", expected_browser))
    ok = (
        final_url.scheme.lower() in ("http", "https")
        and bool(final_url.hostname)
        and ready in ("interactive", "complete")
        and actual_browser == expected_browser
        and bool(str(res.output.get("title", "")).strip())
        and _same_site_family(requested_url, curr_url)
    )
    return VerificationResult(
        verified=ok,
        summary=(
            f"Verified browser navigated to '{curr_url}' (title='{res.output.get('title')}')."
            if ok
            else f"Browser navigation to '{params.get('url')}' could not be verified."
        ),
        evidence={
            "current_url": curr_url,
            "requested_url": requested_url,
            "title": res.output.get("title"),
            "ready_state": ready,
            "requested_browser": expected_browser,
            "actual_browser": actual_browser,
            "text_length": res.output.get("text_length", 0),
        },
        failure_reason=None if ok else f"Unexpected browser URL '{curr_url}' or incomplete DOM state.",
    )


def _exec_search(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    session = _session_for(params, ctx)
    out = session.search(
        query=params["query"],
        search_url=params.get("search_url"),
        timeout_ms=ctx.config.browser_timeout_ms,
    )
    return ToolExecutionResult(success=True, output=out)


def _ver_search(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    curr_url = str(res.output.get("current_url", ""))
    preview = str(res.output.get("visible_text_preview", ""))
    ok = bool(curr_url) and len(preview) > 0
    return VerificationResult(
        verified=ok,
        summary=f"Verified search results rendered for query '{params['query']}' at '{curr_url}'.",
        evidence=res.output,
        failure_reason=None if ok else "Search results page returned empty visible DOM content.",
    )


def _exec_youtube_first_result(
    params: Dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    session = _session_for(params, ctx)
    result = session.youtube_first_result(
        query=params["query"],
        action=params.get("action", "search"),
        timeout_ms=ctx.config.browser_timeout_ms,
        position=int(params.get("position", 1)),
    )
    return ToolExecutionResult(success=True, output=result)


def _exec_add_to_wishlist(
    params: Dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    session = _session_for(params, ctx)
    output = session.add_to_wishlist(
        site=params["site"],
        query=params["query"],
        timeout_ms=ctx.config.browser_timeout_ms,
    )
    output["engine"] = session.engine_name()
    return ToolExecutionResult(success=True, output=output)


def _exec_wikipedia_research(
    params: Dict[str, Any], ctx: ToolContext
) -> ToolExecutionResult:
    session = _session_for(params, ctx)
    result = session.wikipedia_research(
        query=params["query"],
        timeout_ms=ctx.config.browser_timeout_ms,
    )
    return ToolExecutionResult(success=True, output=result)


def _ver_wikipedia_research(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    output = res.output
    page_url = urllib.parse.urlsplit(str(output.get("current_url", "")))
    page_host = (page_url.hostname or "").lower()
    results = output.get("results", [])
    summary = str(output.get("summary", "")).strip()
    valid_results = isinstance(results, list) and bool(results)
    if valid_results:
        for item in results:
            if (
                not isinstance(item, dict)
                or not str(item.get("title", "")).strip()
                or not str(item.get("content", "")).strip()
            ):
                valid_results = False
                break
            article_url = urllib.parse.urlsplit(str(item.get("url", "")))
            article_host = (article_url.hostname or "").lower()
            if (
                article_url.scheme not in ("http", "https")
                or (
                    article_host != "wikipedia.org"
                    and not article_host.endswith(".wikipedia.org")
                )
                or str(item["title"]).strip() not in summary
                or str(item.get("url", "")) not in summary
                or str(item["content"]).strip() not in summary
                or str(item.get("url", "")) != str(output.get("current_url", ""))
            ):
                valid_results = False
                break
    result_count = output.get("result_count")
    verified = (
        page_host == "wikipedia.org"
        or page_host.endswith(".wikipedia.org")
    ) and (
        str(output.get("query", "")) == str(params.get("query", ""))
        and valid_results
        and bool(summary)
        and isinstance(result_count, int)
        and not isinstance(result_count, bool)
        and result_count == len(results)
    )
    reason = None if verified else (
        "Wikipedia research did not return matching, verifiable article titles and URLs."
    )
    return VerificationResult(
        verified=verified,
        summary=(
            f"Verified {len(results) if isinstance(results, list) else 0} retrieved "
            f"Wikipedia result(s) for '{params['query']}'."
            if verified
            else reason
        ),
        evidence={
            "query": output.get("query"),
            "current_url": output.get("current_url"),
            "result_count": result_count,
            "results": results,
            "summary_length": len(summary),
        },
        failure_reason=reason,
    )


def _ver_add_to_wishlist(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    output = res.output
    product_url = urllib.parse.urlsplit(str(output.get("product_url", "")))
    current_url = urllib.parse.urlsplit(str(output.get("current_url", "")))
    host = (product_url.hostname or "").lower()
    expected_host = {"amazon": "amazon.", "flipkart": "flipkart."}.get(
        str(params.get("site", "")).lower()
    )
    verified = (
        expected_host is not None
        and expected_host in host
        and product_url.scheme in ("http", "https")
        and bool(str(output.get("product_title", "")).strip())
        and str(output.get("query", "")) == str(params.get("query", ""))
        and str(output.get("site", "")).lower() == str(params.get("site", "")).lower()
        and 0 <= float(output.get("rating", -1)) <= 5
        and bool(output.get("wishlist_added"))
        and current_url == product_url
    )
    reason = None if verified else (
        "The selected product, visible rating, or wishlist confirmation did not match "
        "the requested site and query."
    )
    return VerificationResult(
        verified=verified,
        summary=(
            f"Verified '{output.get('product_title')}' (rating {output.get('rating')}) "
            "was added to the wishlist."
            if verified
            else reason
        ),
        evidence={
            "site": output.get("site"),
            "query": output.get("query"),
            "product_title": output.get("product_title"),
            "rating": output.get("rating"),
            "product_url": output.get("product_url"),
            "current_url": output.get("current_url"),
            "wishlist_added": output.get("wishlist_added"),
        },
        failure_reason=reason,
    )


def _ver_youtube_first_result(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    output = res.output
    url = urllib.parse.urlsplit(str(output.get("current_url", "")))
    action = str(params.get("action", "search"))
    position = int(params.get("position", 1))
    video_host = (url.hostname or "").lower()
    youtube_host = video_host == "youtube.com" or video_host.endswith(".youtube.com")
    result_count = int(output.get("result_count", 0))
    if action == "search":
        search_query = urllib.parse.parse_qs(url.query).get("search_query", [""])[0]
        result_titles = output.get("result_titles", [])
        verified = (
            url.scheme in ("http", "https")
            and youtube_host
            and url.path == "/results"
            and search_query.casefold() == str(params.get("query", "")).casefold()
            and str(output.get("query", "")).casefold()
            == str(params.get("query", "")).casefold()
            and str(output.get("action", "")) == "search"
            and result_count > 0
            and isinstance(result_titles, list)
            and result_count == len(result_titles)
            and int(output.get("selected_position", -1)) == 0
            and not bool(output.get("playback_started"))
        )
        reason = None if verified else (
            "YouTube search results for the exact requested query were not verified."
        )
        return VerificationResult(
            verified=verified,
            summary=(
                f"Verified {result_count} YouTube search results for '{params['query']}'."
                if verified
                else reason
            ),
            evidence={
                "query": output.get("query"),
                "current_url": output.get("current_url"),
                "result_count": result_count,
                "result_titles": output.get("result_titles"),
            },
            failure_reason=reason,
        )
    player_ready = int(output.get("player_ready_state", 0)) >= 1
    selected_url = urllib.parse.urlsplit(
        str(output.get("selected_result_url", ""))
    )
    selected_host = (selected_url.hostname or "").lower()
    selected_video_id = urllib.parse.parse_qs(selected_url.query).get("v", [""])[0]
    opened_video_id = urllib.parse.parse_qs(url.query).get("v", [""])[0]
    result_titles = output.get("result_titles", [])
    selected_title_matches = (
        isinstance(result_titles, list)
        and len(result_titles) == result_count
        and 1 <= position <= len(result_titles)
        and str(result_titles[position - 1])
        == str(output.get("video_title", ""))
    )
    correct_video = (
        url.scheme in ("http", "https")
        and youtube_host
        and url.path == "/watch"
        and (
            not str(params.get("query", "")).strip()
            or str(output.get("query", "")).casefold()
            == str(params.get("query", "")).casefold()
        )
        and str(output.get("action", "")) == action
        and bool(str(output.get("video_title", "")).strip())
        and result_count >= position
        and int(output.get("selected_position", position)) == position
        and selected_title_matches
        and selected_url.scheme in ("http", "https")
        and (
            selected_host == "youtube.com"
            or selected_host.endswith(".youtube.com")
        )
        and selected_video_id
        and opened_video_id == selected_video_id
        and str(output.get("video_url", "")) == str(output.get("current_url", ""))
        and player_ready
    )
    playback_started = (
        bool(output.get("playback_started"))
        and float(output.get("current_time", 0)) > 0
        and int(output.get("player_ready_state", 0)) >= 2
    )
    verified = correct_video and (action != "play" or playback_started)
    reason = None
    if not correct_video:
        reason = f"YouTube result {position} and its video player were not verified."
    elif action == "play" and not playback_started:
        reason = "The requested YouTube video opened, but playback did not start."
    return VerificationResult(
        verified=verified,
        summary=(
            f"Verified YouTube video '{output.get('video_title')}' "
            + ("started playback." if action == "play" else "opened in the player.")
            if verified
            else reason or "YouTube action could not be verified."
        ),
        evidence={
            "query": output.get("query"),
            "video_title": output.get("video_title"),
            "selected_result_url": output.get("selected_result_url"),
            "video_url": output.get("video_url"),
            "current_url": output.get("current_url"),
            "player_ready_state": output.get("player_ready_state"),
            "playback_started": output.get("playback_started"),
            "current_time": output.get("current_time"),
            "result_count": output.get("result_count"),
            "selected_position": output.get("selected_position"),
        },
        failure_reason=reason,
    )


def _exec_click(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    session = _session_for(params, ctx)
    out = session.click_element(params["selector"], timeout_ms=ctx.config.browser_timeout_ms)
    return ToolExecutionResult(success=True, output=out)


def _ver_click(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    ok = bool(res.output.get("clicked")) and bool(res.output.get("current_url"))
    return VerificationResult(
        verified=ok,
        summary=f"Verified click on '{params['selector']}' (active URL='{res.output.get('current_url')}').",
        evidence=res.output,
        failure_reason=None if ok else "Element click could not be verified.",
    )


def _exec_scroll(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    session = _session_for(params, ctx)
    direction = params.get("direction", "down")
    amount = int(params.get("amount", 500))
    out = session.scroll_page(direction=direction, amount=amount)
    return ToolExecutionResult(success=True, output=out)


def _ver_scroll(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    ok = "scroll_y_after" in res.output and bool(res.output.get("current_url"))
    return VerificationResult(
        verified=ok,
        summary=(
            f"Verified scroll ({res.output.get('direction')}): "
            f"scrollY {res.output.get('scroll_y_before')} -> {res.output.get('scroll_y_after')}."
        ),
        evidence=res.output,
    )


def _exec_read_content(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    session = _session_for(params, ctx)
    out = session.read_visible_content()
    return ToolExecutionResult(success=True, output=out)


def _ver_read_content(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    ok = "visible_text" in res.output and bool(res.output.get("current_url"))
    return VerificationResult(
        verified=ok,
        summary=f"Verified DOM text extraction ({res.output.get('text_length', 0)} chars) from '{res.output.get('current_url')}'.",
        evidence={
            "current_url": res.output.get("current_url"),
            "title": res.output.get("title"),
            "text_length": res.output.get("text_length"),
            "headings": res.output.get("headings"),
        },
        failure_reason=None if ok else "Could not read visible text from active browser DOM.",
    )


def _exec_fill_form(params: Dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    session = _session_for(params, ctx)
    out = session.fill_form(
        fields=params["fields"],
        submit=bool(params.get("submit", False)),
    )
    return ToolExecutionResult(success=True, output=out)


def _ver_fill_form(
    params: Dict[str, Any], res: ToolExecutionResult, ctx: ToolContext
) -> VerificationResult:
    expected_fields: Dict[str, str] = params["fields"]
    verified_vals: Dict[str, str] = res.output.get("verified_values", {})
    all_matched = all(str(verified_vals.get(k)) == str(v) for k, v in expected_fields.items())
    return VerificationResult(
        verified=all_matched,
        summary=(
            f"Verified {len(verified_vals)} form field(s) populated in DOM."
            if all_matched
            else "One or more form fields did not retain expected values."
        ),
        evidence=res.output,
        failure_reason=None if all_matched else "DOM input values did not match requested form values.",
    )


def register_browser_tools(registry: ToolRegistry) -> None:
    """Register all real browser automation tools into ToolRegistry."""
    supported = (
        PlaywrightBrowserSession.is_installed()
        or SeleniumChromiumBrowserSession.is_installed()
    )

    registry.register(
        ToolDefinition(
            name="browser.open_url",
            category=ToolCategory.BROWSER,
            description="Open a URL in the real browser (Playwright / Chromium) and verify DOM load state and title.",
            capabilities=["open_url", "navigate_browser", "visit_website"],
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1},
                    "browser": {"type": "string", "enum": list(BROWSER_CHANNELS)},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "engine": {"type": "string"},
                    "browser_channel": {"type": "string"},
                    "requested_url": {"type": "string"},
                    "current_url": {"type": "string"},
                    "title": {"type": "string"},
                    "ready_state": {"type": "string"},
                    "visible_text_preview": {"type": "string"},
                },
                "required": ["engine", "current_url", "title"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "A real browser engine (Playwright or Chromium WebDriver) must be installed.",
                "URL must be a valid http:// or https:// address.",
            ],
            verification_description="Confirm browser current_url, document.readyState, and absence of CAPTCHA blocks.",
            preconditions_fn=_browser_precondition,
            execute_fn=_exec_open_url,
            verify_fn=_ver_open_url,
            error_handler_fn=_browser_error_handler,
            supported_on_current_platform=supported,
        )
    )

    registry.register(
        ToolDefinition(
            name="browser.search",
            category=ToolCategory.BROWSER,
            description="Search for a query using the real browser and verify rendered search results.",
            capabilities=["web_search", "browser_search", "search_website"],
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "search_url": {"type": "string"},
                    "browser": {"type": "string", "enum": list(BROWSER_CHANNELS)},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "engine": {"type": "string"},
                    "query": {"type": "string"},
                    "current_url": {"type": "string"},
                    "title": {"type": "string"},
                    "visible_text_preview": {"type": "string"},
                },
                "required": ["engine", "query", "current_url"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=["Real browser engine must be available."],
            verification_description="Verify search results page URL and non-empty visible DOM text.",
            preconditions_fn=_browser_precondition,
            execute_fn=_exec_search,
            verify_fn=_ver_search,
            error_handler_fn=_browser_error_handler,
            supported_on_current_platform=supported,
        )
    )

    registry.register(
        ToolDefinition(
            name="browser.add_to_wishlist",
            category=ToolCategory.BROWSER,
            description=(
                "Search Amazon or Flipkart, select the highest-rated product using review counts "
                "to compare equally rated products, "
                "and add it to a wishlist only when the action and confirmation are identifiable."
            ),
            capabilities=["product_search", "wishlist_add", "shopping_wishlist"],
            input_schema={
                "type": "object",
                "properties": {
                    "site": {"type": "string", "enum": ["amazon", "flipkart"]},
                    "query": {"type": "string", "minLength": 1},
                    "browser": {"type": "string", "enum": list(BROWSER_CHANNELS)},
                },
                "required": ["site", "query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "engine": {"type": "string"},
                    "site": {"type": "string"},
                    "query": {"type": "string"},
                    "product_title": {"type": "string"},
                    "rating": {"type": "number"},
                    "review_count": {"type": "integer", "minimum": 0},
                    "product_url": {"type": "string"},
                    "current_url": {"type": "string"},
                    "wishlist_added": {"type": "boolean"},
                },
                "required": [
                    "engine",
                    "site",
                    "query",
                    "product_title",
                    "rating",
                    "review_count",
                    "product_url",
                    "current_url",
                    "wishlist_added",
                ],
            },
            permission_level=PermissionLevel.CONSEQUENTIAL_WRITE,
            precondition_descriptions=[
                "Amazon or Flipkart is open and a real browser is available.",
                "Visible product ratings and wishlist controls must be present.",
                "Authentication and CAPTCHA challenges must be completed manually.",
            ],
            verification_description=(
                "Verify the selected product has a visible rating and the site confirms "
                "wishlist addition."
            ),
            preconditions_fn=_browser_precondition,
            execute_fn=_exec_add_to_wishlist,
            verify_fn=_ver_add_to_wishlist,
            error_handler_fn=_browser_error_handler,
            supported_on_current_platform=supported,
        )
    )

    registry.register(
        ToolDefinition(
            name="browser.wikipedia_research",
            category=ToolCategory.BROWSER,
            description=(
                "Search Wikipedia, open the highest-ranked matching article, and extract "
                "its title, URL, and visible lead content."
            ),
            capabilities=["wikipedia_search", "research_extract", "article_titles_urls"],
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "browser": {"type": "string", "enum": list(BROWSER_CHANNELS)},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "engine": {"type": "string"},
                    "query": {"type": "string"},
                    "current_url": {"type": "string"},
                    "title": {"type": "string"},
                    "result_count": {"type": "integer"},
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                                "content": {"type": "string"},
                                "excerpt": {"type": "string"},
                            },
                            "required": ["title", "url", "content", "excerpt"],
                        },
                    },
                    "summary": {"type": "string"},
                },
                "required": [
                    "engine",
                    "query",
                    "current_url",
                    "title",
                    "result_count",
                    "results",
                    "summary",
                ],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=[
                "A real browser must be open on Wikipedia.",
                "Wikipedia results must expose article titles and canonical URLs.",
            ],
            verification_description=(
                "Verify the selected Wikipedia article title, URL, and extracted content."
            ),
            preconditions_fn=_browser_precondition,
            execute_fn=_exec_wikipedia_research,
            verify_fn=_ver_wikipedia_research,
            error_handler_fn=_browser_error_handler,
            supported_on_current_platform=supported,
        )
    )

    registry.register(
        ToolDefinition(
            name="browser.youtube_first_result",
            category=ToolCategory.BROWSER,
            description=(
                "Search YouTube and optionally open or play the first, second, or third "
                "playable result; verify results or playback without bypassing consent, CAPTCHA, or sign-in."
            ),
            capabilities=[
                "youtube_search_results",
                "youtube_search_first_result",
                "youtube_select_result",
                "youtube_play_video",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "action": {"type": "string", "enum": ["search", "play", "open"]},
                    "position": {"type": "integer", "minimum": 1, "maximum": 3},
                    "browser": {"type": "string", "enum": list(BROWSER_CHANNELS)},
                },
                "required": ["query", "action"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "engine": {"type": "string"},
                    "query": {"type": "string"},
                    "action": {"type": "string"},
                    "result_count": {"type": "integer"},
                    "result_titles": {"type": "array", "items": {"type": "string"}},
                    "selected_position": {"type": "integer"},
                    "video_title": {"type": "string"},
                    "selected_result_url": {"type": "string"},
                    "video_url": {"type": "string"},
                    "current_url": {"type": "string"},
                    "title": {"type": "string"},
                    "player_ready_state": {"type": "integer"},
                    "playback_started": {"type": "boolean"},
                    "current_time": {"type": "number"},
                },
                "required": [
                    "engine",
                    "query",
                    "action",
                    "result_count",
                    "result_titles",
                    "selected_position",
                    "video_title",
                    "selected_result_url",
                    "video_url",
                    "current_url",
                    "title",
                    "player_ready_state",
                    "playback_started",
                    "current_time",
                ],
            },
            permission_level=PermissionLevel.CONSEQUENTIAL_WRITE,
            precondition_descriptions=[
                "A real browser is available and YouTube is reachable.",
                "Any website consent, CAPTCHA, or sign-in challenge must be resolved by the user.",
            ],
            verification_description=(
                "Verify exact-query result visibility, selected video position, and "
                "HTML5 playback for play requests."
            ),
            preconditions_fn=_browser_precondition,
            execute_fn=_exec_youtube_first_result,
            verify_fn=_ver_youtube_first_result,
            error_handler_fn=_browser_error_handler,
            supported_on_current_platform=supported,
        )
    )

    registry.register(
        ToolDefinition(
            name="browser.click",
            category=ToolCategory.BROWSER,
            description="Click an accessible element on the current browser page by CSS selector or visible text.",
            capabilities=["browser_click", "click_element", "click_link"],
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "minLength": 1},
                    "browser": {"type": "string", "enum": list(BROWSER_CHANNELS)},
                },
                "required": ["selector"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "engine": {"type": "string"},
                    "selector": {"type": "string"},
                    "clicked": {"type": "boolean"},
                    "current_url": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["selector", "clicked", "current_url"],
            },
            permission_level=PermissionLevel.CONSEQUENTIAL_WRITE,
            precondition_descriptions=[
                "Browser must have an active page and target element must be visible."
            ],
            verification_description="Verify click interaction succeeded and capture updated DOM URL/title.",
            preconditions_fn=_browser_precondition,
            execute_fn=_exec_click,
            verify_fn=_ver_click,
            error_handler_fn=_browser_error_handler,
            supported_on_current_platform=supported,
        )
    )

    registry.register(
        ToolDefinition(
            name="browser.scroll",
            category=ToolCategory.BROWSER,
            description="Scroll the active browser page up, down, top, or bottom and verify window.scrollY.",
            capabilities=["browser_scroll", "scroll_page"],
            input_schema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["down", "up", "top", "bottom"],
                    },
                    "amount": {"type": "integer", "minimum": 1},
                    "browser": {"type": "string", "enum": list(BROWSER_CHANNELS)},
                },
                "required": ["direction"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "engine": {"type": "string"},
                    "direction": {"type": "string"},
                    "scroll_y_before": {"type": "integer"},
                    "scroll_y_after": {"type": "integer"},
                    "current_url": {"type": "string"},
                },
                "required": ["direction", "scroll_y_before", "scroll_y_after"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=["Browser must have an open page."],
            verification_description="Read window.scrollY from DOM before and after scrolling.",
            preconditions_fn=_browser_precondition,
            execute_fn=_exec_scroll,
            verify_fn=_ver_scroll,
            error_handler_fn=_browser_error_handler,
            supported_on_current_platform=supported,
        )
    )

    registry.register(
        ToolDefinition(
            name="browser.read_content",
            category=ToolCategory.BROWSER,
            description="Read visible text, title, and headings from the active browser DOM.",
            capabilities=["read_webpage", "extract_page_text", "browser_read"],
            input_schema={
                "type": "object",
                "properties": {
                    "browser": {"type": "string", "enum": list(BROWSER_CHANNELS)}
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "engine": {"type": "string"},
                    "current_url": {"type": "string"},
                    "title": {"type": "string"},
                    "visible_text": {"type": "string"},
                    "headings": {"type": "array"},
                    "text_length": {"type": "integer"},
                },
                "required": ["current_url", "title", "visible_text", "text_length"],
            },
            permission_level=PermissionLevel.SAFE_READ,
            precondition_descriptions=["Browser must have an open page."],
            verification_description="Confirm non-empty current_url and extracted DOM body text.",
            preconditions_fn=_browser_precondition,
            execute_fn=_exec_read_content,
            verify_fn=_ver_read_content,
            error_handler_fn=_browser_error_handler,
            supported_on_current_platform=supported,
        )
    )

    registry.register(
        ToolDefinition(
            name="browser.fill_form",
            category=ToolCategory.BROWSER,
            description="Fill accessible form fields in the active browser page and verify DOM input values (requires approval for sensitive fields or submission).",
            capabilities=["fill_form", "browser_form"],
            input_schema={
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "minProperties": 1,
                    },
                    "submit": {"type": "boolean"},
                    "browser": {"type": "string", "enum": list(BROWSER_CHANNELS)},
                },
                "required": ["fields"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "engine": {"type": "string"},
                    "filled_count": {"type": "integer"},
                    "verified_values": {"type": "object"},
                    "submitted": {"type": "boolean"},
                    "current_url": {"type": "string"},
                },
                "required": ["filled_count", "verified_values", "current_url"],
            },
            permission_level=PermissionLevel.CONSEQUENTIAL_WRITE,
            precondition_descriptions=[
                "Browser must have an active page containing the specified form fields."
            ],
            verification_description="Read back each input element's DOM .value property and confirm exact match.",
            preconditions_fn=_browser_precondition,
            execute_fn=_exec_fill_form,
            verify_fn=_ver_fill_form,
            error_handler_fn=_browser_error_handler,
            supported_on_current_platform=supported,
        )
    )
