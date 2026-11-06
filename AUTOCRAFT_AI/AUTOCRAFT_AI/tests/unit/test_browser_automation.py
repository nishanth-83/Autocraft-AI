from __future__ import annotations

import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from backend.config import AgentConfig
from backend.tools.base import ToolContext, ToolExecutionResult
from backend.tools.browser import (
    PlaywrightBrowserSession,
    SeleniumChromiumBrowserSession,
    _validate_url,
    _ver_add_to_wishlist,
    _ver_open_url,
    _ver_wikipedia_research,
    _ver_youtube_first_result,
    _shopping_add_to_wishlist,
    _select_highest_rated_product,
    _wikipedia_research,
    register_browser_tools,
)
from backend.tools.registry import ToolRegistry


class _BodyLocator:
    def count(self) -> int:
        return 1

    def wait_for(self, state: str, timeout: int) -> None:
        assert state == "visible"

    def inner_text(self, timeout: int) -> str:
        return "Loaded after navigation."


class _PageAfterAbortedNavigation:
    url = "https://www.amazon.com/"
    goto_calls = 0

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        from playwright.sync_api import Error as PlaywrightError

        self.goto_calls += 1
        raise PlaywrightError("net::ERR_ABORTED; navigation committed")

    def locator(self, selector: str) -> _BodyLocator:
        assert selector == "body"
        return _BodyLocator()

    def wait_for_function(self, expression: str, timeout: int) -> None:
        assert "document.readyState" in expression

    def title(self) -> str:
        return "Amazon"

    def content(self) -> str:
        return "<html><body>Amazon</body></html>"

    def evaluate(self, expression: str) -> str:
        return "complete"


class _FakeYoutubeSession:
    def is_available(self) -> bool:
        return True

    def engine_name(self) -> str:
        return "playwright"

    def youtube_first_result(
        self,
        query: str,
        action: str = "search",
        timeout_ms: int = 20000,
        position: int = 1,
    ) -> dict:
        return {
            "engine": "playwright",
            "query": query,
            "action": action,
            "result_count": 3,
            "result_titles": [
                f"Tamil songs - result {index}" for index in range(1, 4)
            ],
            "selected_position": 0 if action == "search" else position,
            "video_title": "" if action == "search" else f"Tamil songs - result {position}",
            "video_url": (
                "https://www.youtube.com/results?search_query=Tamil+songs"
                if action == "search"
                else f"https://www.youtube.com/watch?v=video-{position}"
            ),
            "selected_result_url": (
                ""
                if action == "search"
                else f"https://www.youtube.com/watch?v=video-{position}"
            ),
            "current_url": (
                "https://www.youtube.com/results?search_query=Tamil+songs"
                if action == "search"
                else f"https://www.youtube.com/watch?v=video-{position}"
            ),
            "title": "Tamil songs",
            "player_ready_state": 4,
            "playback_started": action == "play",
            "current_time": 1.2 if action == "play" else 0.0,
        }


class _FakeYoutubeLocator:
    def __init__(self, page, selector: str, index: int = 0) -> None:
        self.page = page
        self.selector = selector
        self.index = index

    @property
    def first(self):
        return self

    def wait_for(self, state: str, timeout: int) -> None:
        assert state == "visible"

    def fill(self, query: str) -> None:
        self.page.search_query = query

    def press(self, key: str) -> None:
        assert key == "Enter"
        self.page.url = (
            "https://www.youtube.com/results?"
            + urllib.parse.urlencode({"search_query": self.page.search_query})
        )

    def evaluate_all(self, expression: str):
        assert "closest('ytd-video-renderer')" in expression
        return self.page.results

    def nth(self, index: int):
        return _FakeYoutubeLocator(self.page, self.selector, index)

    def click(self, timeout: int = 0) -> None:
        if self.selector.startswith("ytd-video-renderer"):
            result = next(
                item for item in self.page.results if item["index"] == self.index
            )
            self.page.selected_result = result
            self.page.url = result["href"]
            self.page.player_state = {
                "ready_state": 4,
                "paused": True,
                "current_time": 0.0,
            }
        elif self.selector == "button.ytp-play-button":
            if self.page.playback_succeeds:
                self.page.player_state["paused"] = False
                self.page.player_state["current_time"] = 1.0

    def count(self) -> int:
        return 0 if self.selector in (
            "ytd-consent-bump-v2",
            "tp-yt-paper-dialog #content",
        ) else 1

    def is_visible(self) -> bool:
        return self.count() > 0

    def inner_text(self, timeout: int = 0) -> str:
        return "YouTube search results"

    def evaluate(self, expression: str):
        return self.page.player_state


class _FakeYoutubePage:
    def __init__(
        self,
        *,
        no_results: bool = False,
        playback_succeeds: bool = True,
        current_results_query: str = "",
    ) -> None:
        self.url = (
            "https://www.youtube.com/results?"
            + urllib.parse.urlencode({"search_query": current_results_query})
            if current_results_query
            else "https://www.youtube.com/"
        )
        self.search_query = current_results_query
        self.no_results = no_results
        self.playback_succeeds = playback_succeeds
        self.selected_result = None
        self.player_state = {
            "ready_state": 0,
            "paused": True,
            "current_time": 0.0,
        }
        self.results = [
            {
                "index": 0,
                "href": "https://www.youtube.com/watch?v=advert",
                "title": "Sponsored video",
                "visible": True,
                "ad": True,
            },
            {
                "index": 1,
                "href": "https://www.youtube.com/watch?v=hidden",
                "title": "Hidden video",
                "visible": False,
                "ad": False,
            },
            *[
                {
                    "index": index,
                    "href": f"https://www.youtube.com/watch?v=video{index - 1}",
                    "title": f"Video result {index - 1}",
                    "visible": True,
                    "ad": False,
                }
                for index in range(2, 5)
            ],
        ]

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        return _FakeYoutubeLocator(self, selector)

    def wait_for_function(
        self, expression: str, arg=None, timeout: int = 0
    ) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        if "search_query" in expression:
            assert arg == ["Anirudh best songs", 1] or arg in (
                ["Tamil songs", 1],
                ["Tamil songs", 2],
                ["Tamil songs", 3],
            )
            if self.no_results:
                raise PlaywrightTimeoutError("no video results")
        elif "readyState >= 1" in expression:
            self.player_state["ready_state"] = 4
        elif "currentTime > 0" in expression:
            if not self.playback_succeeds:
                raise PlaywrightTimeoutError("playback stalled")

    def wait_for_url(self, pattern, timeout: int = 0) -> None:
        assert pattern.search(self.url)

    def title(self) -> str:
        return (
            f"{self.selected_result['title']} - YouTube"
            if self.selected_result
            else "YouTube search results"
        )

    def content(self) -> str:
        return "<html><body>YouTube</body></html>"


class _FakeWishlistSession:
    def is_available(self) -> bool:
        return True

    def engine_name(self) -> str:
        return "playwright"

    def add_to_wishlist(
        self, site: str, query: str, timeout_ms: int = 20000
    ) -> dict:
        product_url = (
            "https://www.amazon.com/dp/B0TEST1234"
            if site == "amazon"
            else "https://www.flipkart.com/p/itm123"
        )
        return {
            "site": site,
            "query": query,
            "product_title": "Portable Bluetooth Speaker",
            "rating": 4.8,
            "review_count": 132,
            "product_url": product_url,
            "current_url": product_url,
            "wishlist_added": True,
        }


class _FakeWikipediaSession:
    def is_available(self) -> bool:
        return True

    def engine_name(self) -> str:
        return "playwright"

    def wikipedia_research(self, query: str, timeout_ms: int = 20000) -> dict:
        article_content = (
            "Computer security is the protection of computer systems and networks "
            "from information disclosure, theft of or damage to their hardware, "
            "software, or electronic data."
        )
        return {
            "engine": "playwright",
            "query": query,
            "current_url": "https://en.wikipedia.org/wiki/Computer_security",
            "title": "Computer security - Wikipedia",
            "result_count": 1,
            "results": [
                {
                    "title": "Computer security",
                    "url": "https://en.wikipedia.org/wiki/Computer_security",
                    "content": article_content,
                    "excerpt": article_content[:450],
                }
            ],
            "summary": (
                "Wikipedia article research for: cybersecurity\n"
                "Title: Computer security\n"
                "URL: https://en.wikipedia.org/wiki/Computer_security\n"
                f"Relevant content:\n{article_content}"
            ),
        }


class _FakeWikipediaSearchBox:
    @property
    def first(self):
        return self

    def wait_for(self, state: str, timeout: int) -> None:
        assert state == "visible"

    def fill(self, query: str) -> None:
        self.query = query

    def press(self, key: str) -> None:
        assert key == "Enter"
        self.page.url = (
            "https://en.wikipedia.org/wiki/Computer_security"
            if self.page.direct_article
            else "https://en.wikipedia.org/w/index.php?search="
            + urllib.parse.quote_plus(self.query)
        )

    def __init__(self, page) -> None:
        self.page = page


class _FakeWikipediaPage:
    url = "https://www.wikipedia.org/"

    def __init__(self, *, direct_article: bool = False) -> None:
        self.search_box = _FakeWikipediaSearchBox(self)
        self.direct_article = direct_article
        self.navigation_calls = []

    def locator(self, selector: str):
        assert selector == "#searchInput, input[name='search'], input[type='search']"
        return self.search_box

    def wait_for_function(self, expression: str, timeout: int) -> None:
        if "li.mw-search-result .mw-search-result-heading a" in expression:
            return
        assert "document.querySelector('#mw-content-text')" in expression
        assert self.url == "https://en.wikipedia.org/wiki/Computer_security"

    def evaluate(self, expression: str):
        if "document.querySelectorAll('li.mw-search-result')" in expression:
            return [
                {
                    "title": "Help:Searching",
                    "url": "https://en.wikipedia.org/wiki/Help:Searching",
                    "excerpt": "How to search Wikipedia.",
                },
                {
                    "title": "Computer security",
                    "url": "https://en.wikipedia.org/wiki/Computer_security",
                    "excerpt": "Computer security protects computer systems.",
                },
            ]
        assert "document.querySelector('#firstHeading')" in expression
        return {
            "title": "Computer security",
            "url": self.url,
            "content": (
                "Computer security is the protection of computer systems and "
                "networks from information disclosure, theft of or damage to "
                "their hardware, software, or electronic data."
            ),
        }

    def goto(self, url: str, timeout: int, wait_until: str) -> None:
        assert timeout == 1000
        assert wait_until == "domcontentloaded"
        self.navigation_calls.append(url)
        self.url = url

    def title(self) -> str:
        return "Computer security - Wikipedia"


class _FakeShoppingLocator:
    def __init__(self, page, selector: str, index: int = 0) -> None:
        self.page = page
        self.selector = selector
        self.index = index

    @property
    def first(self):
        return self

    def nth(self, index: int):
        return _FakeShoppingLocator(self.page, self.selector, index)

    def wait_for(self, state: str, timeout: int) -> None:
        assert state == "visible"

    def fill(self, query: str) -> None:
        self.page.search_query = query

    def press(self, key: str) -> None:
        assert key == "Enter"
        query = self.page.search_query.replace(" ", "+")
        if self.page.site == "amazon":
            self.page.url = f"https://www.amazon.com/s?k={query}"
        else:
            self.page.url = f"https://www.flipkart.com/search?q={query}"

    def click(self, timeout: int = 0) -> None:
        if self.selector in ("a[href*='/dp/'], a[href*='/gp/product/']", "a[href*='/p/']"):
            self.page.url = self.page.candidates[self.index]["href"]
        elif self.selector == "button, [role=\"button\"], a[role=\"button\"]":
            if self.page.site == "amazon":
                self.page.list_menu_open = True
            else:
                self.page.wishlist_added = True
        elif self.selector == "[role=\"menuitem\"], [role=\"option\"], button, a":
            self.page.wishlist_added = True

    def count(self) -> int:
        return 0

    def inner_text(self, timeout: int = 0) -> str:
        if self.selector == "body":
            return "Added to wishlist" if self.page.wishlist_added else "Speaker product"
        return ""

    def evaluate_all(self, expression: str):
        assert self.page.site == "amazon"
        return [{"index": 0, "text": "Wish List", "visible": True}]


class _FakeShoppingPage:
    def __init__(self, site: str) -> None:
        self.site = site
        self.url = f"https://www.{site}.com/"
        self.search_query = ""
        self.wishlist_added = False
        self.list_menu_open = False
        self.product_url = (
            "https://www.amazon.com/dp/B0TEST1234"
            if site == "amazon"
            else "https://www.flipkart.com/p/itm123"
        )
        self.candidates = [
            {
                "index": 0,
                "title": "Good speaker",
                "rating": 4.7,
                "review_count": 900,
                "href": self.product_url,
                "visible": True,
                "sponsored": False,
            },
            {
                "index": 1,
                "title": "Highest-rated speaker",
                "rating": 4.9,
                "review_count": 120,
                "href": self.product_url,
                "visible": True,
                "sponsored": False,
            },
        ]

    def locator(self, selector: str):
        return _FakeShoppingLocator(self, selector)

    def wait_for_function(self, expression: str, arg=None, timeout: int = 0) -> None:
        if "searchParams.get(parameter)" in expression:
            self.asserted_search_argument = arg
            assert "out of 5" not in expression
        elif "querySelectorAll(selector)" in expression:
            assert arg in ("a[href*='/dp/'], a[href*='/gp/product/']", "a[href*='/p/']")
        else:
            assert self.wishlist_added

    def evaluate(self, expression: str, argument=None):
        if expression.startswith("(selector) => Array.from"):
            return self.candidates
        if expression.startswith("() => Array.from"):
            label = "Remove from Wishlist" if self.wishlist_added else (
                "Add to List" if self.site == "amazon" else "Add to Wishlist"
            )
            return [{"index": 0, "label": label, "visible": True}]
        assert "loginRoute" in expression
        return False

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        assert state == "domcontentloaded"

    def content(self) -> str:
        return "<html><body>Speaker product</body></html>"

    def title(self) -> str:
        return "Speaker product"


class TestBrowserAutomation(unittest.TestCase):
    def test_browser_tool_registration_does_not_launch_a_browser(self) -> None:
        registry = ToolRegistry()
        with (
            patch.object(
                PlaywrightBrowserSession,
                "is_available",
                side_effect=AssertionError("registration must not launch a browser"),
            ),
            patch.object(
                SeleniumChromiumBrowserSession,
                "is_available",
                side_effect=AssertionError("registration must not launch a browser"),
            ),
        ):
            register_browser_tools(registry)
        self.assertIn("browser.open_url", {tool.name for tool in registry.list_tools()})

    def test_url_normalization_accepts_http_sites_and_rejects_malformed_hosts(self) -> None:
        self.assertEqual(_validate_url("amazon.com"), "https://amazon.com")
        self.assertEqual(_validate_url("HTTPS://www.python.org/path"), "https://www.python.org/path")
        self.assertEqual(_validate_url("http://localhost:8000/"), "http://localhost:8000/")
        for url in ("******python.org", "https://bad_host.example", "http:python.org"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _validate_url(url)

    def test_err_aborted_navigation_uses_committed_page_without_retrying(self) -> None:
        page = _PageAfterAbortedNavigation()
        session = PlaywrightBrowserSession()
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="test",
                step_id="step_1",
            )
            try:
                with patch.object(session, "_ensure_page", return_value=page):
                    result = session.open_url("https://www.amazon.com/", timeout_ms=1000)
            finally:
                session.close()
            self.assertEqual(page.goto_calls, 1)
            self.assertEqual(result["current_url"], "https://www.amazon.com/")
            self.assertEqual(result["title"], "Amazon")
            self.assertTrue(
                _ver_open_url(
                    {"url": "https://www.amazon.com/"},
                    ToolExecutionResult(success=True, output=result),
                    context,
                ).verified
            )
            redirected = dict(result, current_url="https://amazon-login.invalid/")
            self.assertFalse(
                _ver_open_url(
                    {"url": "https://www.amazon.com/"},
                    ToolExecutionResult(success=True, output=redirected),
                    context,
                ).verified
            )

    def test_youtube_tool_executes_and_verifies_real_requested_result(self) -> None:
        fake_session = _FakeYoutubeSession()
        with tempfile.TemporaryDirectory() as directory:
            config = AgentConfig(
                workspace_root=Path(directory) / "workspace",
                database_path=Path(directory) / "state.db",
            )
            ctx = ToolContext(config=config, task_id="youtube-test", step_id="step_2")
            registry = ToolRegistry()
            with patch("backend.tools.browser.get_browser_session", return_value=fake_session):
                register_browser_tools(registry)
            with patch("backend.tools.browser._session_for", return_value=fake_session):
                for action, position in (("search", 1), ("play", 1), ("play", 2), ("play", 3)):
                    with self.subTest(action=action, position=position):
                        _, execution, verification = registry.dispatch(
                            "browser.youtube_first_result",
                            {"query": "Tamil songs", "action": action, "position": position},
                            ctx,
                        )
                        self.assertTrue(execution.success)
                        self.assertTrue(verification.verified, verification.failure_reason)
                        self.assertEqual(execution.output["query"], "Tamil songs")
                        if action == "search":
                            self.assertNotIn("/watch?", execution.output["current_url"])
                            self.assertFalse(execution.output["playback_started"])
                            self.assertEqual(execution.output["selected_position"], 0)
                        else:
                            self.assertIn(f"/watch?v=video-{position}", execution.output["current_url"])
                            self.assertTrue(execution.output["playback_started"])
                            self.assertEqual(execution.output["selected_position"], position)

    def test_wishlist_tool_executes_for_flipkart_and_amazon(self) -> None:
        fake_session = _FakeWishlistSession()
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="wishlist-sites-test",
                step_id="step_2",
            )
            registry = ToolRegistry()
            with patch("backend.tools.browser.get_browser_session", return_value=fake_session):
                register_browser_tools(registry)
            with patch("backend.tools.browser._session_for", return_value=fake_session):
                for site in ("flipkart", "amazon"):
                    with self.subTest(site=site):
                        _, execution, verification = registry.dispatch(
                            "browser.add_to_wishlist",
                            {"site": site, "query": "best speakers"},
                            context,
                        )
                        self.assertTrue(execution.success)
                        self.assertTrue(verification.verified, verification.failure_reason)
                        self.assertEqual(execution.output["site"], site)
                        self.assertEqual(execution.output["query"], "best speakers")
                        self.assertTrue(execution.output["wishlist_added"])

    def test_real_wishlist_flow_selects_rated_product_and_verifies_both_sites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="wishlist-page-flow-test",
                step_id="step_2",
            )
            for site in ("flipkart", "amazon"):
                with self.subTest(site=site):
                    page = _FakeShoppingPage(site)
                    output = _shopping_add_to_wishlist(
                        page, site, "best-rated speakers", timeout_ms=1000
                    )
                    verification = _ver_add_to_wishlist(
                        {"site": site, "query": "best-rated speakers"},
                        ToolExecutionResult(success=True, output=output),
                        context,
                    )

                    self.assertEqual(page.search_query, "best-rated speakers")
                    self.assertEqual(page.asserted_search_argument[1], "best-rated speakers")
                    self.assertTrue(page.wishlist_added)
                    self.assertEqual(output["product_title"], "Highest-rated speaker")
                    self.assertEqual(output["rating"], 4.9)
                    self.assertTrue(verification.verified, verification.failure_reason)

    def test_wishlist_ranking_uses_rating_then_review_count(self) -> None:
        candidates = [
            {"title": "More reviews", "rating": 4.7, "review_count": 1800},
            {"title": "Highest rating", "rating": 4.8, "review_count": 90},
            {"title": "Tied rating, stronger evidence", "rating": 4.8, "review_count": 1200},
        ]
        self.assertEqual(
            _select_highest_rated_product(candidates)["title"],
            "Tied rating, stronger evidence",
        )

    def test_wikipedia_research_tool_extracts_and_verifies_article_results(self) -> None:
        fake_session = _FakeWikipediaSession()
        with tempfile.TemporaryDirectory() as directory:
            ctx = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="wikipedia-test",
                step_id="step_1",
            )
            registry = ToolRegistry()
            with patch("backend.tools.browser.get_browser_session", return_value=fake_session):
                register_browser_tools(registry)
            with patch("backend.tools.browser._session_for", return_value=fake_session):
                _, execution, verification = registry.dispatch(
                    "browser.wikipedia_research",
                    {"query": "cybersecurity"},
                    ctx,
                )

        self.assertTrue(execution.success, execution.error_message)
        self.assertTrue(verification.verified, verification.failure_reason)
        self.assertEqual(execution.output["results"][0]["title"], "Computer security")
        self.assertIn("https://en.wikipedia.org/wiki/Computer_security", execution.output["summary"])
        self.assertIn("Computer security is the protection", execution.output["results"][0]["content"])

    def test_wikipedia_search_opens_first_article_and_extracts_lead_content(self) -> None:
        page = _FakeWikipediaPage()
        result = _wikipedia_research(page, "cybersecurity", timeout_ms=1000)

        self.assertEqual(result["query"], "cybersecurity")
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(
            page.navigation_calls,
            ["https://en.wikipedia.org/wiki/Computer_security"],
        )
        self.assertEqual(result["current_url"], page.navigation_calls[0])
        self.assertEqual(result["results"][0]["title"], "Computer security")
        self.assertIn(
            "Computer security is the protection",
            result["results"][0]["content"],
        )
        self.assertIn("Relevant content:", result["summary"])

    def test_wikipedia_search_handles_direct_article_redirect(self) -> None:
        page = _FakeWikipediaPage(direct_article=True)
        result = _wikipedia_research(page, "cybersecurity", timeout_ms=1000)

        self.assertEqual(
            result["current_url"],
            "https://en.wikipedia.org/wiki/Computer_security",
        )
        self.assertEqual(result["results"][0]["title"], "Computer security")
        self.assertEqual(page.navigation_calls, [])

    def test_wikipedia_verification_rejects_non_wikipedia_article_urls(self) -> None:
        output = _FakeWikipediaSession().wikipedia_research("cybersecurity")
        output["results"][0]["url"] = "https://en.wikipedia.org.attacker.test/fake"
        with tempfile.TemporaryDirectory() as directory:
            ctx = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="wikipedia-invalid-test",
                step_id="step_1",
            )
            verification = _ver_wikipedia_research(
                {"query": "cybersecurity"},
                ToolExecutionResult(success=True, output=output),
                ctx,
            )
        self.assertFalse(verification.verified)
        output = _FakeWikipediaSession().wikipedia_research("cybersecurity")
        output["summary"] = "Wikipedia search completed."
        with tempfile.TemporaryDirectory() as directory:
            ctx = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="wikipedia-missing-summary-test",
                step_id="step_1",
            )
            verification = _ver_wikipedia_research(
                {"query": "cybersecurity"},
                ToolExecutionResult(success=True, output=output),
                ctx,
            )
        self.assertFalse(verification.verified)

    def test_youtube_search_only_verifies_query_results_without_opening_a_video(self) -> None:
        fake_session = _FakeYoutubeSession()
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="youtube-search-test",
                step_id="step_2",
            )
            output = fake_session.youtube_first_result("Tamil songs", action="search")
            verification = _ver_youtube_first_result(
                {"query": "Tamil songs", "action": "search", "position": 1},
                ToolExecutionResult(success=True, output=output),
                context,
            )
        self.assertTrue(verification.verified, verification.failure_reason)
        self.assertEqual(output["selected_position"], 0)
        self.assertNotIn("/watch?", output["current_url"])

    def test_youtube_browser_search_does_not_select_or_play_a_video(self) -> None:
        page = _FakeYoutubePage()
        session = PlaywrightBrowserSession()
        session._page = page
        try:
            output = session.youtube_first_result(
                "Anirudh best songs", action="search", timeout_ms=1000
            )
        finally:
            session.close()

        self.assertEqual(page.search_query, "Anirudh best songs")
        self.assertIsNone(page.selected_result)
        self.assertEqual(output["selected_position"], 0)
        self.assertFalse(output["playback_started"])
        self.assertEqual(output["result_titles"], [
            "Video result 1", "Video result 2", "Video result 3"
        ])

    def test_youtube_browser_plays_the_requested_visible_video_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="youtube-video-selection-test",
                step_id="step_2",
            )
            for position in (1, 2, 3):
                with self.subTest(position=position):
                    page = _FakeYoutubePage()
                    session = PlaywrightBrowserSession()
                    session._page = page
                    try:
                        output = session.youtube_first_result(
                            "Tamil songs",
                            action="play",
                            position=position,
                            timeout_ms=1000,
                        )
                    finally:
                        session.close()
                    verification = _ver_youtube_first_result(
                        {
                            "query": "Tamil songs",
                            "action": "play",
                            "position": position,
                        },
                        ToolExecutionResult(success=True, output=output),
                        context,
                    )

                    self.assertEqual(page.search_query, "Tamil songs")
                    self.assertEqual(
                        page.selected_result["title"],
                        f"Video result {position}",
                    )
                    self.assertEqual(output["selected_position"], position)
                    self.assertTrue(output["playback_started"])
                    self.assertTrue(
                        verification.verified, verification.failure_reason
                    )

    def test_youtube_ordinal_only_uses_existing_search_results(self) -> None:
        page = _FakeYoutubePage(current_results_query="Tamil songs")
        session = PlaywrightBrowserSession()
        session._page = page
        try:
            output = session.youtube_first_result(
                "", action="play", position=2, timeout_ms=1000
            )
        finally:
            session.close()

        with tempfile.TemporaryDirectory() as directory:
            verification = _ver_youtube_first_result(
                {"query": "", "action": "play", "position": 2},
                ToolExecutionResult(success=True, output=output),
                ToolContext(
                    config=AgentConfig(
                        workspace_root=Path(directory) / "workspace",
                        database_path=Path(directory) / "state.db",
                    ),
                    task_id="youtube-current-results-test",
                    step_id="step_1",
                ),
            )
        self.assertEqual(output["query"], "Tamil songs")
        self.assertEqual(output["selected_position"], 2)
        self.assertEqual(page.selected_result["title"], "Video result 2")
        self.assertTrue(output["playback_started"])
        self.assertTrue(verification.verified, verification.failure_reason)

    def test_youtube_ordinal_only_without_current_results_fails_clearly(self) -> None:
        session = PlaywrightBrowserSession()
        session._page = _FakeYoutubePage()
        try:
            with self.assertRaisesRegex(LookupError, "No active YouTube search results"):
                session.youtube_first_result(
                    "", action="play", position=1, timeout_ms=1000
                )
        finally:
            session.close()

    def test_youtube_no_results_and_stalled_playback_fail_honestly(self) -> None:
        for page, action, message in (
            (_FakeYoutubePage(no_results=True), "search", "visible video result"),
            (
                _FakeYoutubePage(playback_succeeds=False),
                "play",
                "did not start playback",
            ),
        ):
            session = PlaywrightBrowserSession()
            session._page = page
            try:
                with self.subTest(action=action), self.assertRaisesRegex(
                    (LookupError, RuntimeError), message
                ):
                    session.youtube_first_result(
                        "Tamil songs",
                        action=action,
                        timeout_ms=10,
                    )
            finally:
                session.close()

    def test_youtube_open_action_does_not_claim_playback(self) -> None:
        output = _FakeYoutubeSession().youtube_first_result("Python tutorials", "open")
        output["playback_started"] = False
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="youtube-open-test",
                step_id="step_2",
            )
            verification = _ver_youtube_first_result(
                {"query": "Python tutorials", "action": "open"},
                ToolExecutionResult(success=True, output=output),
                context,
            )
            self.assertTrue(verification.verified)
            output["current_url"] = "https://www.youtube.com/"
            output["video_url"] = output["current_url"]
            self.assertFalse(
                _ver_youtube_first_result(
                    {"query": "Python tutorials", "action": "open"},
                    ToolExecutionResult(
                        success=True,
                        output={
                            **output,
                            "current_url": "https://evil-youtube.com/watch?v=video-id",
                            "video_url": "https://evil-youtube.com/watch?v=video-id",
                        },
                    ),
                    context,
                ).verified
            )

    def test_wishlist_action_requires_matching_product_and_site_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                config=AgentConfig(
                    workspace_root=Path(directory) / "workspace",
                    database_path=Path(directory) / "state.db",
                ),
                task_id="wishlist-test",
                step_id="step_2",
            )
            params = {"site": "flipkart", "query": "best rated speakers"}
            good_output = _FakeWishlistSession().add_to_wishlist(
                "flipkart", params["query"]
            )
            verified = _ver_add_to_wishlist(
                params,
                ToolExecutionResult(success=True, output=good_output),
                context,
            )
            self.assertTrue(verified.verified, verified.failure_reason)

            bad_output = dict(good_output, wishlist_added=False)
            failed_verification = _ver_add_to_wishlist(
                params,
                ToolExecutionResult(success=True, output=bad_output),
                context,
            )
            self.assertFalse(failed_verification.verified)


if __name__ == "__main__":
    unittest.main()
