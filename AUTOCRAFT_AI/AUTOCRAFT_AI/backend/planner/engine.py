"""
Deterministic Offline Self-Planning Engine for AUTOCRAFT AI.
Converts natural language goals (in English, Tamil, or Thanglish) into ordered,
executable PlanStep objects with explicit dependencies, tool bindings, parameters,
and verification conditions.

Works 100% offline without requiring Ollama or external AI APIs.
Never confuses understanding a task with completing it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.planner.nlp import (
    detect_language,
    normalize_clause_to_english,
    split_compound_clauses,
)


class PlanningError(ValueError):
    """Raised when a goal is empty, malformed, or asks for unsupported capabilities."""


@dataclass
class PlanStep:
    """Single executable step produced by the Self-Planning Engine."""

    step_id: str
    step_order: int
    title: str
    tool_name: str
    parameters: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)
    expected_condition: str = ""
    requires_approval: bool = False
    approval_reason: Optional[str] = None
    max_retries: int = 2
    fallback_tool: Optional[str] = None
    fallback_parameters: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_order": self.step_order,
            "title": self.title,
            "tool_name": self.tool_name,
            "parameters": self.parameters,
            "depends_on": self.depends_on,
            "expected_condition": self.expected_condition,
            "requires_approval": self.requires_approval,
            "approval_reason": self.approval_reason,
            "max_retries": self.max_retries,
            "fallback_tool": self.fallback_tool,
            "fallback_parameters": self.fallback_parameters,
            "status": "PENDING",
            "attempt_count": 0,
            "execution_output": {},
            "verification": {},
            "error": {},
        }


@dataclass
class GeneratedPlan:
    """Structured executable plan produced for a user goal."""

    goal: str
    detected_language: str
    normalized_clauses: List[str]
    steps: List[PlanStep]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "detected_language": self.detected_language,
            "normalized_clauses": self.normalized_clauses,
            "steps": [s.to_dict() for s in self.steps],
        }


def _extract_quoted_or_tail(text: str, prefix_pattern: str) -> Optional[str]:
    q_match = re.search(r"['\"](.+?)['\"]", text)
    if q_match:
        return q_match.group(1)
    tail_match = re.search(prefix_pattern, text, flags=re.IGNORECASE)
    if tail_match:
        return tail_match.group(1).strip()
    return None


def _number_sequence(text: str) -> Optional[str]:
    match = re.search(
        r"\b(?:numbers?\s+)?from\s+(-?\d+)\s+to\s+(-?\d+)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None

    start, end = (int(match.group(1)), int(match.group(2)))
    count = abs(end - start) + 1
    if count > 10000:
        raise PlanningError("A Notepad number sequence cannot exceed 10,000 values.")
    step = 1 if end >= start else -1
    separator = ", " if re.search(r"\bcomma(?:-|\s*)separated\b", text, re.IGNORECASE) else "\n"
    return separator.join(str(value) for value in range(start, end + step, step))


def _alphabet_sequence(text: str) -> Optional[str]:
    reverse = bool(re.search(r"\b(?:reverse|backwards|backward)\b", text, re.IGNORECASE))
    match = re.search(
        r"\b(?:letters?\s+)?from\s+([a-z])\s+to\s+([a-z])\b|"
        r"\b([a-z])\s+to\s+([a-z])\b",
        text,
        re.IGNORECASE,
    )
    if match:
        start, end = match.group(1, 2) if match.group(1) else match.group(3, 4)
        start_code, end_code = ord(start.lower()), ord(end.lower())
        direction = 1 if end_code >= start_code else -1
        letters = [
            chr(code)
            for code in range(start_code, end_code + direction, direction)
        ]
    elif re.search(r"\balphabet\b", text, re.IGNORECASE):
        letters = list("abcdefghijklmnopqrstuvwxyz")
    else:
        return None

    if reverse:
        letters.reverse()
    if re.search(
        r"\b(?:each|every)\s+(?:letter|character)\s+(?:on|to)\s+(?:a\s+)?new\s+line\b|"
        r"\bone\s+(?:letter|character)\s+per\s+line\b|"
        r"\beach\s+on\s+(?:a\s+)?new\s+line\b",
        text,
        re.IGNORECASE,
    ):
        separator = "\n"
    elif re.search(r"\bcomma(?:-|\s*)separated\b", text, re.IGNORECASE):
        separator = ", "
    elif re.search(r"\bspace\s+separated\b", text, re.IGNORECASE):
        separator = " "
    else:
        separator = ""
    return separator.join(letters)


def _browser_open_steps(
    url: str,
    clause: str,
    start_order: int,
    step_id: str,
    dependencies: List[str],
    browser: Optional[str],
    max_retries: int,
) -> List[PlanStep]:
    parameters: Dict[str, Any] = {"url": url}
    if browser:
        parameters["browser"] = browser
    steps = [
        PlanStep(
            step_id=step_id,
            step_order=start_order,
            title=f"Navigate browser to {url}",
            tool_name="browser.open_url",
            parameters=parameters,
            depends_on=dependencies,
            expected_condition=f"Browser loaded {url} and verified its final URL and title",
            max_retries=max_retries,
        )
    ]
    if re.search(r"\b(?:read|extract)\b|inspect\s+content", clause, re.IGNORECASE):
        steps.append(
            PlanStep(
                step_id=f"step_{start_order + 1}",
                step_order=start_order + 1,
                title="Read visible text from loaded webpage",
                tool_name="browser.read_content",
                parameters={},
                depends_on=[step_id],
                expected_condition="Visible DOM text extracted from page",
                max_retries=max_retries,
            )
        )
    return steps


class SelfPlanningEngine:
    """
    Deterministic rule- and template-based planner that maps English, Tamil, and Thanglish
    goals into ordered tool invocations with dependency wiring.
    """

    def __init__(self, default_max_retries: int = 2) -> None:
        self.default_max_retries = default_max_retries

    def create_plan(self, goal: str) -> GeneratedPlan:
        if not goal or not isinstance(goal, str) or not goal.strip():
            raise PlanningError("Goal cannot be empty. Provide a clear natural language instruction.")

        cleaned_goal = goal.strip()
        lang = detect_language(cleaned_goal)
        wikipedia_request = self._wikipedia_research_request(cleaned_goal)
        if wikipedia_request is not None:
            query, filename = wikipedia_request
            return GeneratedPlan(
                goal=cleaned_goal,
                detected_language=lang,
                normalized_clauses=[cleaned_goal],
                steps=[
                    PlanStep(
                        step_id="step_1",
                        step_order=1,
                        title="Open Wikipedia",
                        tool_name="browser.open_url",
                        parameters={"url": "https://www.wikipedia.org/"},
                        expected_condition="Wikipedia loaded with a verified URL and title",
                        max_retries=0,
                    ),
                    PlanStep(
                        step_id="step_2",
                        step_order=2,
                        title=f"Search Wikipedia for '{query}' and extract actual results",
                        tool_name="browser.wikipedia_research",
                        parameters={"query": query},
                        depends_on=["step_1"],
                        expected_condition=(
                            "Relevant Wikipedia search results, titles, URLs, and excerpts were retrieved"
                        ),
                        max_retries=0,
                    ),
                    PlanStep(
                        step_id="step_3",
                        step_order=3,
                        title=f"Save Wikipedia research to '{filename}' in Downloads",
                        tool_name="fs.create_file",
                        parameters={
                            "path": f"Downloads\\{filename}",
                            "content": "",
                            "content_from_step": {
                                "step_id": "step_2",
                                "output_key": "summary",
                            },
                        },
                        depends_on=["step_2"],
                        expected_condition=(
                            f"File 'Downloads\\\\{filename}' exists and matches the retrieved research summary"
                        ),
                        max_retries=0,
                    ),
                ],
            )

        shopping_request = self._shopping_wishlist_request(cleaned_goal)
        if shopping_request is not None:
            site, query = shopping_request
            browser = self._browser_channel_for_clause(cleaned_goal.lower())
            domain = "www.amazon.com" if site == "amazon" else "www.flipkart.com"
            open_parameters: Dict[str, Any] = {"url": f"https://{domain}/"}
            wishlist_parameters: Dict[str, Any] = {"site": site, "query": query}
            if browser:
                open_parameters["browser"] = browser
                wishlist_parameters["browser"] = browser
            return GeneratedPlan(
                goal=cleaned_goal,
                detected_language=lang,
                normalized_clauses=[cleaned_goal],
                steps=[
                    PlanStep(
                        step_id="step_1",
                        step_order=1,
                        title=f"Open {site.capitalize()}",
                        tool_name="browser.open_url",
                        parameters=open_parameters,
                        expected_condition=(
                            f"{site.capitalize()} loaded with a verified URL and page title"
                        ),
                        max_retries=0,
                    ),
                    PlanStep(
                        step_id="step_2",
                        step_order=2,
                        title=f"Search {site.capitalize()} for '{query}' and add the highest rated product to the wishlist",
                        tool_name="browser.add_to_wishlist",
                        parameters=wishlist_parameters,
                        depends_on=["step_1"],
                        expected_condition=(
                            "A visibly rated product is selected and wishlist confirmation is observed"
                        ),
                        max_retries=0,
                    ),
                ],
            )

        youtube_request = self._youtube_first_result_request(cleaned_goal)
        if youtube_request is not None:
            query, action, position = youtube_request
            browser = self._browser_channel_for_clause(cleaned_goal.lower())
            action_params: Dict[str, Any] = {
                "query": query,
                "action": action,
                "position": position,
            }
            youtube_steps: List[PlanStep] = []
            action_step_id = "step_1"
            action_dependencies: List[str] = []
            if query:
                browser_params: Dict[str, Any] = {
                    "url": "https://www.youtube.com/"
                }
                if browser:
                    browser_params["browser"] = browser
                youtube_steps.append(
                    PlanStep(
                        step_id="step_1",
                        step_order=1,
                        title="Open YouTube",
                        tool_name="browser.open_url",
                        parameters=browser_params,
                        expected_condition=(
                            "YouTube search page loaded with a verified URL and title"
                        ),
                        max_retries=0,
                    )
                )
                action_step_id = "step_2"
                action_dependencies = ["step_1"]
            if browser:
                action_params["browser"] = browser
            youtube_steps.append(
                PlanStep(
                    step_id=action_step_id,
                    step_order=len(youtube_steps) + 1,
                    title=(
                        f"Search YouTube for '{query}'"
                        if action == "search"
                        else f"Search YouTube for '{query}' and {action} result {position}"
                        if query
                        else f"{action.capitalize()} result {position} from the current YouTube results"
                    ),
                    tool_name="browser.youtube_first_result",
                    parameters=action_params,
                    depends_on=action_dependencies,
                    expected_condition=(
                        "YouTube search results for the requested query are visible"
                        if action == "search"
                        else (
                            f"YouTube result {position} opened and video playback verified"
                            if action == "play"
                            else f"YouTube result {position} opened and video player loaded"
                        )
                    ),
                    max_retries=0,
                )
            )
            return GeneratedPlan(
                goal=cleaned_goal,
                detected_language=lang,
                normalized_clauses=[cleaned_goal],
                steps=youtube_steps,
            )

        has_drawing_request = bool(
            re.search(
                r"\b(?:draw|sketch)\s+(?:(?:a|an|the)\s+)?(?:simple\s+)?"
                r"(?:circle|rectangle|box|ellipse|oval|triangle|house|landscape)\b",
                cleaned_goal,
                re.IGNORECASE,
            )
        )
        paint_requested = bool(
            re.search(
                r"\b(?:open|launch|start)\s+(?:windows\s+)?paint\b|"
                r"\b(?:draw|sketch)\b.*?\bin\s+(?:windows\s+)?paint\b",
                cleaned_goal,
                re.IGNORECASE,
            )
            or (
                has_drawing_request
                and not re.search(
                    r"\b(notepad|calculator|chrome|edge|browser|youtube|amazon|"
                    r"flipkart|wikipedia|python\.org|file\s+explorer)\b",
                    cleaned_goal,
                    re.IGNORECASE,
                )
            )
        )
        if paint_requested:
            paint_step = self._map_clause_to_steps(
                normalize_clause_to_english(cleaned_goal),
                raw_clause=cleaned_goal,
                start_order=1,
                prev_step_id=None,
            )
            if paint_step and paint_step[0].tool_name == "desktop.paint_launch":
                return GeneratedPlan(
                    goal=cleaned_goal,
                    detected_language=lang,
                    normalized_clauses=[normalize_clause_to_english(cleaned_goal)],
                    steps=paint_step,
                )

        raw_clauses = split_compound_clauses(cleaned_goal)
        if not raw_clauses:
            raise PlanningError("Could not extract any actionable clauses from the goal.")

        normalized_clauses: List[str] = []
        steps: List[PlanStep] = []
        requested_browser = self._browser_channel_for_clause(cleaned_goal.lower())

        for clause in raw_clauses:
            norm = normalize_clause_to_english(clause)
            normalized_clauses.append(norm)
            prev_step_id = steps[-1].step_id if steps else None
            clause_steps = self._map_clause_to_steps(
                norm,
                raw_clause=clause,
                start_order=len(steps) + 1,
                prev_step_id=prev_step_id,
            )
            if not clause_steps:
                raise PlanningError(
                    f"Unsupported or unrecognized instruction clause: '{clause}'. "
                    "AUTOCRAFT AI only plans tasks backed by verified registered tools."
                )
            for clause_step in clause_steps:
                if requested_browser and clause_step.tool_name.startswith("browser."):
                    clause_step.parameters.setdefault("browser", requested_browser)
            steps.extend(clause_steps)

        return GeneratedPlan(
            goal=cleaned_goal,
            detected_language=lang,
            normalized_clauses=normalized_clauses,
            steps=steps,
        )

    @staticmethod
    def _shopping_wishlist_request(goal: str) -> Optional[tuple[str, str]]:
        site_match = re.search(r"\b(amazon|flipkart)\b", goal, re.IGNORECASE)
        if site_match is None or not re.search(
            r"\b(?:wishlist|wish\s+list|favorites?)\b", goal, re.IGNORECASE
        ):
            return None
        search_match = re.search(
            r"\bsearch(?:\s+for)?\s+(.+?)(?=\s*[,;:]?\s*(?:and\s+)?(?:add|save|put)\b)",
            goal,
            re.IGNORECASE,
        )
        if search_match is None:
            return None
        query = search_match.group(1).strip().strip(" \t\r\n.,;:!?\"'")
        query = re.sub(r"^(?:the|a|an)\s+", "", query, flags=re.IGNORECASE)
        query = re.sub(
            r"\s+(?:on|in)\s+(?:amazon|flipkart)\s*$",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip()
        if not query:
            return None
        return site_match.group(1).lower(), query

    @staticmethod
    def _wikipedia_research_request(goal: str) -> Optional[tuple[str, str]]:
        if not re.search(r"\bwikipedia\b", goal, re.IGNORECASE):
            return None
        if not re.search(r"\b(?:save|write|store)\b", goal, re.IGNORECASE) or not re.search(
            r"\b(?:file|downloads?|text)\b|\.txt\b", goal, re.IGNORECASE
        ):
            return None
        search_match = re.search(
            r"\b(?:search|research|look\s+up)\s+"
            r"(?:(?:wikipedia)\s+)?(?:for\s+)?(.+?)"
            r"(?=\s+(?:in|on)\s+wikipedia\b|\s+(?:and\s+)?(?:save|write|store)\b|$)",
            goal,
            re.IGNORECASE,
        )
        if search_match is None:
            return None
        query = search_match.group(1).strip().strip(" \t\r\n.,;:!?\"'")
        query = re.sub(
            r"^(?:the\s+)?(?:(?:top\s+\d+|top)\s+)?(?:best\s+)?",
            "",
            query,
            flags=re.IGNORECASE,
        )
        if not query:
            return None
        filename_match = re.search(
            r"\b(?:as|named|called)\s+['\"]?([A-Za-z0-9_-]+\.txt)\b",
            goal,
            re.IGNORECASE,
        )
        filename = (
            filename_match.group(1)
            if filename_match
            else (
                "cybersecurity_research.txt"
                if re.search(r"cyber[\s-]?security", query, re.IGNORECASE)
                else re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_") + ".txt"
            )
        )
        return query, filename

    @staticmethod
    def _youtube_first_result_request(
        goal: str,
    ) -> Optional[tuple[str, str, int]]:
        youtube_named = bool(re.search(r"\byoutube\b", goal, re.IGNORECASE))
        ordinal = r"(?:first|1st|1|second|2nd|2|third|3rd|3)"
        action_matches = list(re.finditer(
            rf"\b(play|watch|open)\s+(?:the\s+)?({ordinal})\b"
            r"(?:\s+(?:video|result|one|song))?",
            goal,
            re.IGNORECASE,
        ))
        action_match = action_matches[0] if action_matches else None
        search_match = re.search(
            r"\bsearch\s+(?:on\s+)?youtube(?:\s+for)?\s+(.+)",
            goal,
            re.IGNORECASE,
        )
        if search_match is None:
            search_match = re.search(
                r"\bsearch(?:\s+for)?\s+(.+)",
                goal,
                re.IGNORECASE,
            )
        if search_match is None:
            if action_match is None:
                return None
            action_is_negated = bool(
                re.search(
                    r"\b(?:without|never|don't|do\s+not|not)\b[^.!?]*$",
                    goal[: action_match.start()],
                    re.IGNORECASE,
                )
            )
            if action_is_negated:
                return None
            action = action_match.group(1).lower()
            action = "play" if action == "watch" else action
            ordinal_value = action_match.group(2).lower()
            position = (
                1 if ordinal_value in ("first", "1st", "1")
                else 2 if ordinal_value in ("second", "2nd", "2")
                else 3
            )
            return "", action, position

        query_text = search_match.group(1)
        quoted_query = re.match(r"""^\s*(['"])(.*?)\1""", query_text)
        if quoted_query:
            query = quoted_query.group(2).strip()
            query_start = search_match.start(1)
            query_end = query_start + quoted_query.end()
            action_match = next(
                (
                    match
                    for match in action_matches
                    if not query_start <= match.start() < query_end
                ),
                None,
            )
        else:
            query_text = re.split(
                r"""(?ix)
                \s+(?:and\s+)?(?:show|display|view)\s+(?:(?:the\s+)?search\s+)?results?\b
                | \s+without\b
                | \s+(?:and\s+)?(?:do\s+not|don't|never)\s+
                    (?:intentionally\s+)?(?:select|play|watch)\b
                | \s+(?:and\s+)?(?:play|watch|open)\s+(?:the\s+)?"""
                + ordinal
                + r"""\b
                | \.\s*(?=(?:show|display|view|without|play|watch)\b)
                """,
                query_text,
                maxsplit=1,
            )[0]
            query = query_text.strip().strip(" \t\r\n.,;:!?\"'")
        query = re.sub(r"\s+(?:in|on)\s+youtube$", "", query, flags=re.IGNORECASE)
        if not query:
            return None

        action_is_negated = bool(
            action_match
            and re.search(
                r"\b(?:without|never|don't|do\s+not|not)\b[^.!?]*$",
                goal[: action_match.start()],
                re.IGNORECASE,
            )
        )
        if not youtube_named and action_match is None:
            return None
        if action_match is None or action_is_negated:
            return query, "search", 1
        action = action_match.group(1).lower()
        action = "play" if action == "watch" else action
        ordinal_value = action_match.group(2).lower()
        position = (
            1 if ordinal_value in ("first", "1st", "1")
            else 2 if ordinal_value in ("second", "2nd", "2")
            else 3
        )
        return query, action, position

    @staticmethod
    def _browser_channel_for_clause(clause: str) -> Optional[str]:
        if re.search(r"\b(?:microsoft\s+)?edge\b", clause):
            return "msedge"
        if re.search(r"\b(?:google\s+)?chrome\b", clause):
            return "chrome"
        return None

    def _map_clause_to_steps(
        self,
        clause: str,
        raw_clause: str,
        start_order: int,
        prev_step_id: Optional[str],
    ) -> List[PlanStep]:
        c_lower = clause.lower().strip()
        deps = [prev_step_id] if prev_step_id else []
        step_id = f"step_{start_order}"

        if any(phrase in c_lower for phrase in ("search", "google for", "bing for", "look up")) and (
            "browser" in c_lower or "web" in c_lower or "search for" in c_lower or "search" in c_lower
        ):
            query = _extract_quoted_or_tail(
                clause,
                r"(?:search(?:\s+browser|\s+the\s+web|\s+website)?(?:\s+for)?|look\s+up)\s+(.+)$",
            )
            if query:
                return [
                    PlanStep(
                        step_id=step_id,
                        step_order=start_order,
                        title=f"Search browser for '{query}'",
                        tool_name="browser.search",
                        parameters={"query": query},
                        depends_on=deps,
                        expected_condition=f"Browser displays search results for '{query}'",
                        max_retries=self.default_max_retries,
                    )
                ]

        if any(
            phrase in c_lower
            for phrase in (
                "read page",
                "read webpage",
                "read visible content",
                "read browser",
                "extract page text",
            )
        ):
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title="Read visible content from active browser page",
                    tool_name="browser.read_content",
                    parameters={},
                    depends_on=deps,
                    expected_condition="Visible DOM text and headings extracted",
                    max_retries=self.default_max_retries,
                )
            ]

        # ------------------------------------------------------------------
        # 1. DESKTOP: NOTEPAD
        # ------------------------------------------------------------------
        notepad_open_request = bool(
            re.search(r"\b(?:open|launch|start)\s+(?:(?:the|windows)\s+)?notepad\b", c_lower)
        )
        generation_clause = re.sub(r"(['\"]).*?\1", " ", clause)
        number_payload = _number_sequence(generation_clause)
        alphabet_payload = _alphabet_sequence(generation_clause)
        notepad_requested = bool(
            notepad_open_request
            or re.search(r"\bnotepad\b.*\b(?:type|write|enter)\b", c_lower)
            or re.search(r"\b(?:type|write|enter)\b.*\bin\s+notepad\b", c_lower)
            or number_payload is not None
            or alphabet_payload is not None
        )
        if notepad_requested:
            if notepad_open_request and not re.search(
                r"\b(?:type|write|enter|save)\b", c_lower
            ):
                return [
                    PlanStep(
                        step_id=step_id,
                        step_order=start_order,
                        title="Open Windows Notepad",
                        tool_name="desktop.notepad_open",
                        parameters={},
                        depends_on=deps,
                        expected_condition="A visible Notepad window is open",
                        max_retries=0,
                    )
                ]
            text_payload = number_payload
            if text_payload is None:
                text_payload = alphabet_payload
            if text_payload is None:
                content_clause = re.sub(
                    r"\s+(?:and\s+)?save(?:\s+(?:it|this))?\s+(?:as|to)\s+.+$",
                    "",
                    clause,
                    flags=re.IGNORECASE,
                )
                text_payload = _extract_quoted_or_tail(
                    content_clause,
                    r"(?:type|write|enter|with\s+text|saying)\s+(.+?)(?:\s+in\s+notepad)?$",
                )
            if not text_payload:
                text_payload = "Hello from AUTOCRAFT AI"

            save_match = re.search(
                r"(?:save(?:\s+(?:it|this))?\s+(?:as|to)|into)\s+"
                r"(Downloads[\\/]+)?([A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]+)"
                r"(?:\s+(?:in|under)\s+Downloads)?",
                clause,
                re.IGNORECASE,
            )
            params: Dict[str, Any] = {"text": text_payload}
            if save_match:
                filename = save_match.group(2)
                in_downloads = bool(save_match.group(1)) or bool(
                    re.search(r"\b(?:in|under)\s+Downloads\b", clause, re.IGNORECASE)
                )
                params["save_path"] = (
                    f"Downloads\\{filename}" if in_downloads else filename
                )

            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Open Notepad and enter text: '{text_payload[:40]}'",
                    tool_name="desktop.notepad_write",
                    parameters=params,
                    depends_on=deps,
                    expected_condition=f"Notepad editor window active and contains '{text_payload}'",
                    max_retries=self.default_max_retries,
                )
            ]

        # ------------------------------------------------------------------
        # 2. DESKTOP: CALCULATOR
        # ------------------------------------------------------------------
        calc_request = re.search(
            r"(?:^|\band\s+|\bplease\s+)(?:calculate|compute|evaluate)\s+(.+)$",
            clause,
            re.IGNORECASE,
        )
        calculator_requested = bool(
            re.search(
                r"\b(?:open|launch|start)\s+(?:(?:the|windows)\s+)?calculator\b",
                c_lower,
            )
            or calc_request
            or re.search(
                r"\b(?:add|subtract|multiply|divide)\b.*\b(?:in|using)\s+(?:the\s+)?calculator\b",
                c_lower,
            )
            or (
                "calculator" in c_lower
                and re.search(r"\d\s*(?:[+\-*/×÷^]|\*\*)\s*\d", c_lower)
            )
        )
        if calculator_requested:
            calculator_open_request = bool(
                re.search(
                    r"\b(?:open|launch|start)\s+(?:(?:the|windows)\s+)?calculator\b",
                    c_lower,
                )
            )
            if calculator_open_request and not calc_request and not re.search(
                r"\b(?:add|subtract|multiply|divide|sin|cos|tan|sqrt|log|ln)\b"
                r"|\d\s*(?:[+\-*/×÷^]|\*\*)\s*\d",
                c_lower,
            ):
                return [
                    PlanStep(
                        step_id=step_id,
                        step_order=start_order,
                        title="Open Windows Calculator",
                        tool_name="desktop.calculator_open",
                        parameters={},
                        depends_on=deps,
                        expected_condition="A visible Windows Calculator window is open",
                        max_retries=0,
                    )
                ]
            # Check verbal forms like "add 25 and 75 in calculator"
            m_add = re.search(r"add\s+(\d+(?:\.\d+)?)\s+(?:and|to)\s+(\d+(?:\.\d+)?)", c_lower)
            m_sub = re.search(r"subtract\s+(\d+(?:\.\d+)?)\s+from\s+(\d+(?:\.\d+)?)", c_lower)
            m_mul = re.search(r"multiply\s+(\d+(?:\.\d+)?)\s+(?:by|and|with)\s+(\d+(?:\.\d+)?)", c_lower)
            m_div = re.search(r"divide\s+(\d+(?:\.\d+)?)\s+by\s+(\d+(?:\.\d+)?)", c_lower)

            expr: Optional[str] = None
            if calc_request:
                expr = calc_request.group(1).strip()
                expr = re.sub(
                    r"\s+(?:in|using)\s+(?:the\s+)?calculator\s*$",
                    "",
                    expr,
                    flags=re.IGNORECASE,
                )
                expr = re.sub(r"\s+(?:in\s+)?(?:degrees?|radians?|deg|rad)\s*$", "", expr, flags=re.IGNORECASE)
            elif re.search(r"\b(?:sin|cos|tan|sqrt|log10|log|ln)\b", c_lower):
                expr_match = re.search(
                    r"\b(?:sin|cos|tan|sqrt|log10|log|ln)\s*(?:\([^)]*\)|-?(?:\d+(?:\.\d*)?|\.\d+))",
                    clause,
                    re.IGNORECASE,
                )
                expr = expr_match.group(0).strip() if expr_match else None
            else:
                expr_match = re.search(
                    r"(\d+(?:\.\d+)?(?:\s*[+\-*/×÷^]\s*(?:\d+(?:\.\d+)?|\([^()]*\)))+)",
                    clause,
                )
                if expr_match:
                    expr = expr_match.group(1).strip()
            if not expr and m_add:
                expr = f"{m_add.group(1)}+{m_add.group(2)}"
            elif not expr and m_sub:
                expr = f"{m_sub.group(2)}-{m_sub.group(1)}"
            elif not expr and m_mul:
                expr = f"{m_mul.group(1)}*{m_mul.group(2)}"
            elif not expr and m_div:
                expr = f"{m_div.group(1)}/{m_div.group(2)}"

            if not expr:
                raise PlanningError(
                    f"Calculator instruction '{raw_clause}' did not contain a valid arithmetic expression."
                )

            angle_mode = (
                "radians"
                if re.search(r"\b(?:radians?|rad)\b", clause, re.IGNORECASE)
                else "degrees"
            )
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Compute '{expr}' in Windows Calculator",
                    tool_name="desktop.calculator_compute",
                    parameters={"expression": expr, "angle_mode": angle_mode},
                    depends_on=deps,
                    expected_condition=f"Calculator display equals evaluated value of {expr}",
                    max_retries=self.default_max_retries,
                )
            ]

        # ------------------------------------------------------------------
        # 3. DESKTOP: PAINT
        # ------------------------------------------------------------------
        paint_requested = bool(
            re.search(r"\b(?:open|launch|start)\s+(?:windows\s+)?paint\b", c_lower)
            or "mspaint" in c_lower
            or re.search(r"\bpaint\b\s+(?:and\s+)?(?:draw|sketch)\b", c_lower)
            or re.search(r"\b(?:draw|sketch)\b.*\bin\s+paint\b", c_lower)
            or (
                re.search(
                    r"\b(?:draw|sketch)\s+(?:(?:a|an|the)\s+)?(?:simple\s+)?"
                    r"(?:circle|rectangle|box|ellipse|oval|triangle|house|landscape)\b",
                    c_lower,
                )
                and not re.search(r"\b(?:browser|website|webpage|chrome|edge)\b", c_lower)
            )
        )
        if paint_requested:
            shape = None
            if re.search(r"\bhouse\b", c_lower):
                shape = "house"
            elif re.search(r"\blandscape\b", c_lower):
                shape = "landscape"
            elif re.search(r"\btriangle\b", c_lower):
                shape = "triangle"
            elif re.search(r"\bcircle\b", c_lower):
                shape = "circle"
            elif any(w in c_lower for w in ("box", "rectangle")):
                shape = "rectangle"
            elif "ellipse" in c_lower or "oval" in c_lower:
                shape = "ellipse"
            elif any(w in c_lower for w in ("draw", "sketch")):
                shape = "rectangle"
            save_match = re.search(
                r"\bsave(?:\s+it)?\s+(?:as|to)\s+([A-Za-z0-9_.-]+\.[A-Za-z0-9]+)",
                clause,
                re.IGNORECASE,
            )
            downloads_requested = bool(
                re.search(r"\b(?:in|to|into)\s+(?:my\s+)?downloads?\b", clause, re.IGNORECASE)
            )
            paint_parameters: Dict[str, Any] = {
                "draw_shape": shape,
                "draw_box": shape == "rectangle",
            }
            if save_match and downloads_requested:
                paint_parameters["save_path"] = "Downloads\\" + save_match.group(1)
            elif save_match:
                paint_parameters["save_path"] = save_match.group(1)
            elif downloads_requested:
                default_name = (
                    f"{shape}.png"
                    if shape in ("house", "landscape", "triangle")
                    else "autocraft_drawing.png"
                )
                paint_parameters["save_path"] = f"Downloads\\{default_name}"
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=(
                        f"Open Windows Paint and draw a {shape}"
                        if shape
                        else "Launch Windows Paint (mspaint.exe)"
                    ),
                    tool_name="desktop.paint_launch",
                    parameters=paint_parameters,
                    depends_on=deps,
                    expected_condition=(
                        f"Paint canvas contains a verified {shape}"
                        if shape
                        else "Paint window handle (MSPaintApp) is visible and active"
                    ),
                    max_retries=0,
                )
            ]

        # ------------------------------------------------------------------
        # 4. DESKTOP: FILE EXPLORER
        # ------------------------------------------------------------------
        if re.search(
            r"\b(?:open|launch|navigate)\s+(?:(?:windows|file)\s+)?explorer\b",
            c_lower,
        ):
            path_match = re.search(
                r"(?:at|to|in|folder|path)\s+([A-Za-z0-9_\-./\\]+)",
                clause,
                re.IGNORECASE,
            )
            target_path = path_match.group(1) if path_match else "."
            if target_path.lower() in ("explorer", "file", "workspace"):
                target_path = "."
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Open Windows File Explorer at '{target_path}'",
                    tool_name="desktop.explorer_open",
                    parameters={"path": target_path},
                    depends_on=deps,
                    expected_condition=f"File Explorer window open for directory '{target_path}'",
                    max_retries=self.default_max_retries,
                )
            ]

        # ------------------------------------------------------------------
        # 5. DESKTOP: VS CODE
        # ------------------------------------------------------------------
        if re.search(
            r"\b(?:open|launch|start)\s+(?:(?:visual studio|vs)\s+)?code\b|\b(?:open|launch|start)\s+(?:visual\s+studio\s+code|vscode)\b",
            c_lower,
        ):
            path_match = re.search(r"(?:at|in|folder|file|open)\s+([A-Za-z0-9_\-./\\]+)$", clause, re.IGNORECASE)
            target_path = path_match.group(1) if path_match and path_match.group(1).lower() not in ("vscode", "code") else "."
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Launch VS Code ({target_path})",
                    tool_name="desktop.vscode_open",
                    parameters={"path": target_path},
                    depends_on=deps,
                    expected_condition="VS Code process (Code.exe) is running",
                    max_retries=1,
                    fallback_tool="desktop.notepad_write",
                    fallback_parameters={"text": f"Opened fallback editor for {target_path}"},
                )
            ]

        # ------------------------------------------------------------------
        # 6. DESKTOP: MICROSOFT OFFICE (Word, Excel, PowerPoint)
        # ------------------------------------------------------------------
        office_match = re.search(
            r"\b(?:open|launch|start)\s+(?:microsoft\s+)?(word|excel|powerpoint)\b",
            c_lower,
        )
        if office_match:
            app_name = office_match.group(1).lower()
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Launch Microsoft {app_name.capitalize()}",
                    tool_name="desktop.office_open",
                    parameters={"application": app_name},
                    depends_on=deps,
                    expected_condition=f"Microsoft {app_name.capitalize()} process is running",
                    max_retries=1,
                )
            ]

        # ------------------------------------------------------------------
        # 7. BROWSER AUTOMATION (open URL, search, click, scroll, read, fill form)
        # ------------------------------------------------------------------
        url_match = re.search(
            r"(https?://[^\s'\"<>]+|www\.[^\s'\"<>]+|"
            r"[a-zA-Z0-9][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9-]+)+(?:/[^\s'\"<>]*)?)",
            clause,
            re.IGNORECASE,
        )
        unqualified_file_reference = bool(
            url_match
            and not url_match.group(1).lower().startswith(("http://", "https://"))
            and re.search(
                r"\.(?:png|jpe?g|gif|bmp|svg|txt|md|pdf|docx?|csv|json|py|js|ts)\b",
                url_match.group(1),
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:file|filename|image|save|saving|downloads?|paint)\b",
                c_lower,
                re.IGNORECASE,
            )
        )
        navigation_intent = bool(
            re.search(
                r"\b(?:open|navigate|visit|browse|chrome|edge|url)\b|go\s+to",
                c_lower,
            )
            or (
                re.search(r"\b(?:read|extract)\b|inspect\s+content", c_lower)
                and re.search(r"\b(?:page|webpage|web|browser|content)\b", c_lower)
            )
        )
        if url_match and navigation_intent and not unqualified_file_reference:
            raw_url = url_match.group(1).rstrip(".,;!?)")
            if not raw_url.lower().startswith(("http://", "https://")):
                raw_url = (
                    "https://www.python.org/"
                    if raw_url.lower().rstrip("/") in ("python.org", "www.python.org")
                    else "https://" + raw_url
                )
            route_text = re.sub(
                r"(https?://[^\s'\"<>]+|www\.[^\s'\"<>]+|"
                r"[a-zA-Z0-9][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9-]+)+(?:/[^\s'\"<>]*)?)",
                "",
                c_lower,
                flags=re.IGNORECASE,
            )
            return _browser_open_steps(
                raw_url,
                clause,
                start_order,
                step_id,
                deps,
                self._browser_channel_for_clause(route_text),
                0,
            )

        # 7b. Browser Scroll
        if "scroll" in c_lower:
            direction = "down"
            for d in ("down", "up", "bottom", "top"):
                if d in c_lower:
                    direction = d
                    break
            amt_match = re.search(r"(\d+)\s*(?:px|pixels)?", c_lower)
            amount = int(amt_match.group(1)) if amt_match else 500
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Scroll browser page {direction}",
                    tool_name="browser.scroll",
                    parameters={"direction": direction, "amount": amount},
                    depends_on=deps,
                    expected_condition="Browser scrollY position updated",
                    max_retries=self.default_max_retries,
                )
            ]

        # 7c. Browser Click
        if c_lower.startswith("click ") or "click element" in c_lower or "click on " in c_lower or "click link" in c_lower:
            sel = _extract_quoted_or_tail(
                clause,
                r"click(?:\s+on|\s+element|\s+link|\s+button)?\s+(.+)$",
            )
            if sel:
                return [
                    PlanStep(
                        step_id=step_id,
                        step_order=start_order,
                        title=f"Click browser element '{sel}'",
                        tool_name="browser.click",
                        parameters={"selector": sel},
                        depends_on=deps,
                        expected_condition=f"Element '{sel}' clicked in active DOM",
                        max_retries=self.default_max_retries,
                    )
                ]

        # 7e. Browser Fill Form
        if "fill form" in c_lower or "fill input" in c_lower:
            # Parse key=value pairs in clause e.g. "fill form #username=alice #email=alice@example.com"
            pairs = re.findall(r"([#.\w\-]+)\s*=\s*(['\"].+?['\"]|[^\s,]+)", clause)
            fields: Dict[str, str] = {}
            for k, v in pairs:
                fields[k] = v.strip("'\"")
            if not fields:
                fields = {"input[type='text']": "sample_value"}
            submit = "submit" in c_lower
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Fill browser form ({len(fields)} field(s))",
                    tool_name="browser.fill_form",
                    parameters={"fields": fields, "submit": submit},
                    depends_on=deps,
                    expected_condition="Form input values verified in browser DOM",
                    max_retries=1,
                )
            ]

        site_match = re.search(
            r"\b(?:open|visit|browse|go\s+to)\s+(?:the\s+)?"
            r"(amazon|flipkart|youtube|wikipedia|python\.org)"
            r"(?:\s+(?:in|using|with)\s+(?:(?:google|microsoft)\s+)?(chrome|edge))?\b",
            c_lower,
        )
        if site_match:
            site, requested_browser = site_match.groups()
            domain = {
                "amazon": "www.amazon.com",
                "flipkart": "www.flipkart.com",
                "youtube": "www.youtube.com",
                "wikipedia": "www.wikipedia.org",
                "python.org": "www.python.org",
            }[site]
            browser_channel = (
                "msedge" if requested_browser == "edge" else "chrome"
            ) if requested_browser else None
            parameters: Dict[str, Any] = {"url": f"https://{domain}/"}
            if browser_channel:
                parameters["browser"] = browser_channel
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=(
                        f"Open {site.capitalize()} in {requested_browser.capitalize()}"
                        if requested_browser
                        else f"Open {site.capitalize()}"
                    ),
                    tool_name="browser.open_url",
                    parameters=parameters,
                    depends_on=deps,
                    expected_condition=f"Browser loaded https://{domain}/",
                    max_retries=0,
                )
            ]

        # ------------------------------------------------------------------
        # 8. FILESYSTEM OPERATIONS
        # ------------------------------------------------------------------
        # 8a. Create Folder / Directory
        if re.search(r"\b(create|make|mkdir)\b.*\b(folder|directory|dir)\b", c_lower) or re.search(
            r"\b(new\s+folder|new\s+directory)\b", c_lower
        ):
            m_name = re.search(
                r"(?:folder|directory|dir|mkdir)\s+(?:named\s+|called\s+)?['\"]?([A-Za-z0-9_\-./\\]+)['\"]?",
                clause,
                re.IGNORECASE,
            )
            folder_path = m_name.group(1) if m_name else "new_folder"
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Create folder '{folder_path}'",
                    tool_name="fs.create_folder",
                    parameters={"path": folder_path},
                    depends_on=deps,
                    expected_condition=f"Directory '{folder_path}' exists on disk",
                    max_retries=1,
                )
            ]

        # 8b. Create or Write File
        if re.search(r"\b(create|write|save|make)\b.*\bfile\b", c_lower) or re.search(
            r"\bwrite\b.*?\bto\s+([A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]+)", c_lower
        ):
            m_file = re.search(
                r"(?:file\s+(?:named\s+|called\s+)?|to\s+|into\s+)['\"]?([A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]+)['\"]?",
                clause,
                re.IGNORECASE,
            )
            if not m_file:
                m_file = re.search(r"([A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]+)", clause)
            file_path = m_file.group(1) if m_file else "output.txt"

            content = _extract_quoted_or_tail(
                clause,
                r"(?:with\s+content|containing|with\s+text|content|saying)\s+(.+)$",
            )
            if content is None:
                # Try pattern: write <content> to <file>
                m_wt = re.search(
                    r"write\s+['\"]?(.+?)['\"]?\s+(?:to|in|into)\s+[A-Za-z0-9_\-./\\]+",
                    clause,
                    re.IGNORECASE,
                )
                content = m_wt.group(1) if m_wt else ""

            overwrite_flag = "overwrite" in c_lower
            tool_name = "fs.write_file" if overwrite_flag else "fs.create_file"
            params_fs: Dict[str, Any] = {"path": file_path, "content": content}
            if tool_name == "fs.create_file" and overwrite_flag:
                params_fs["overwrite"] = True

            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Create file '{file_path}' ({len(content)} chars)",
                    tool_name=tool_name,
                    parameters=params_fs,
                    depends_on=deps,
                    expected_condition=f"File '{file_path}' exists and SHA-256 matches content",
                    max_retries=1,
                )
            ]

        # 8c. Append to File
        if "append" in c_lower:
            m_file = re.search(
                r"(?:to|in|into|file)\s+['\"]?([A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]+)['\"]?",
                clause,
                re.IGNORECASE,
            )
            file_path = m_file.group(1) if m_file else "notes.txt"
            content = _extract_quoted_or_tail(clause, r"append\s+(.+?)\s+(?:to|into)\b") or ""
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Append content to '{file_path}'",
                    tool_name="fs.append_file",
                    parameters={"path": file_path, "content": content},
                    depends_on=deps,
                    expected_condition=f"File '{file_path}' ends with appended text",
                    max_retries=1,
                )
            ]

        # 8d. Read File
        if re.search(r"\b(read|cat|inspect|view)\b.*\b(file|[A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]+)", c_lower):
            m_file = re.search(
                r"(?:file\s+|read\s+|inspect\s+|view\s+)['\"]?([A-Za-z0-9_\-./\\]+(?:\.[A-Za-z0-9]+)?)['\"]?",
                clause,
                re.IGNORECASE,
            )
            if m_file and m_file.group(1).lower() == "file":
                m_file = re.search(r"file\s+['\"]?([A-Za-z0-9_\-./\\]+)['\"]?", clause, re.IGNORECASE)
            file_path = m_file.group(1) if m_file else "output.txt"
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Read file '{file_path}'",
                    tool_name="fs.read_file",
                    parameters={"path": file_path},
                    depends_on=deps,
                    expected_condition=f"Content and SHA-256 of '{file_path}' verified",
                    max_retries=1,
                )
            ]

        # 8e. Move or Rename File/Folder
        if any(w in c_lower for w in ("rename ", "move ")):
            m_mv = re.search(
                r"(?:rename|move)\s+(?:file\s+|folder\s+)?['\"]?([A-Za-z0-9_\-./\\]+)['\"]?\s+(?:to|as|into)\s+['\"]?([A-Za-z0-9_\-./\\]+)['\"]?",
                clause,
                re.IGNORECASE,
            )
            if m_mv:
                src, dst = m_mv.group(1), m_mv.group(2)
                return [
                    PlanStep(
                        step_id=step_id,
                        step_order=start_order,
                        title=f"Move/Rename '{src}' to '{dst}'",
                        tool_name="fs.move_or_rename",
                        parameters={"source": src, "destination": dst},
                        depends_on=deps,
                        expected_condition=f"'{src}' moved to '{dst}'",
                        max_retries=1,
                    )
                ]

        # 8f. Copy File
        if "copy " in c_lower:
            m_cp = re.search(
                r"copy\s+(?:file\s+)?['\"]?([A-Za-z0-9_\-./\\]+)['\"]?\s+(?:to|as|into)\s+['\"]?([A-Za-z0-9_\-./\\]+)['\"]?",
                clause,
                re.IGNORECASE,
            )
            if m_cp:
                src, dst = m_cp.group(1), m_cp.group(2)
                return [
                    PlanStep(
                        step_id=step_id,
                        step_order=start_order,
                        title=f"Copy '{src}' to '{dst}'",
                        tool_name="fs.copy_file",
                        parameters={"source": src, "destination": dst},
                        depends_on=deps,
                        expected_condition=f"'{dst}' exists and matches SHA-256 of '{src}'",
                        max_retries=1,
                    )
                ]

        # 8g. Organize Folder
        if "organize" in c_lower:
            m_org = re.search(
                r"organize\s+(?:folder\s+|directory\s+|files\s+in\s+)?['\"]?([A-Za-z0-9_\-./\\]+)['\"]?",
                clause,
                re.IGNORECASE,
            )
            target_dir = m_org.group(1) if m_org else "."
            if target_dir.lower() in ("folder", "directory", "files", "workspace"):
                target_dir = "."
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"Organize files in '{target_dir}' by category",
                    tool_name="fs.organize_folder",
                    parameters={"path": target_dir},
                    depends_on=deps,
                    expected_condition=f"Files in '{target_dir}' sorted into categorized subfolders",
                    max_retries=1,
                )
            ]

        # 8h. Delete File or Folder (Destructive -> Requires Approval)
        if any(w in c_lower for w in ("delete ", "remove ", "erase ")):
            m_del = re.search(
                r"(?:delete|remove|erase)\s+(?:file\s+|folder\s+|directory\s+|path\s+)?['\"]?([A-Za-z0-9_\-./\\]+)['\"]?",
                clause,
                re.IGNORECASE,
            )
            if m_del:
                target_path = m_del.group(1)
                return [
                    PlanStep(
                        step_id=step_id,
                        step_order=start_order,
                        title=f"Delete '{target_path}' (Approval Required)",
                        tool_name="fs.delete_path",
                        parameters={"path": target_path},
                        depends_on=deps,
                        expected_condition=f"Path '{target_path}' no longer exists on disk",
                        requires_approval=True,
                        approval_reason=f"Deleting '{target_path}' is a destructive filesystem operation.",
                        max_retries=0,
                    )
                ]

        # 8i. List Directory
        if any(w in c_lower for w in ("list files", "list directory", "list folder", "show files")):
            m_ls = re.search(
                r"(?:in|at|folder|directory)\s+['\"]?([A-Za-z0-9_\-./\\]+)['\"]?",
                clause,
                re.IGNORECASE,
            )
            target_dir = m_ls.group(1) if m_ls else "."
            if target_dir.lower() in ("workspace", "directory", "folder", "files"):
                target_dir = "."
            return [
                PlanStep(
                    step_id=step_id,
                    step_order=start_order,
                    title=f"List directory contents of '{target_dir}'",
                    tool_name="fs.list_directory",
                    parameters={"path": target_dir},
                    depends_on=deps,
                    expected_condition=f"Directory listing retrieved for '{target_dir}'",
                    max_retries=1,
                )
            ]

        return []
