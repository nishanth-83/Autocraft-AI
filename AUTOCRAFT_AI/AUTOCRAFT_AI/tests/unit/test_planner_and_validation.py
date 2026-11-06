"""
Unit tests for AUTOCRAFT AI Self-Planning Engine (English, Tamil, Thanglish)
and Executable Plan Validator (empty plans, malformed steps, cycles, schema & security checks).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.config import AgentConfig
from backend.main import build_default_registry
from backend.planner.engine import PlanStep, PlanningError, SelfPlanningEngine
from backend.planner.nlp import detect_language
from backend.planner.validator import PlanValidator


class TestPlannerAndValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws_root = Path(self.temp_dir.name) / "workspace"
        self.config = AgentConfig(
            workspace_root=self.ws_root,
            database_path=self.ws_root / "test.db",
        )
        self.registry = build_default_registry()
        self.planner = SelfPlanningEngine()
        self.validator = PlanValidator(registry=self.registry, config=self.config)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_language_detection_en_ta_thanglish(self) -> None:
        self.assertEqual(detect_language("Open notepad and type hello"), "en")
        self.assertEqual(
            detect_language("நோட்பேட் திறந்து வணக்கம் என்று எழுது"),
            "ta",
        )
        self.assertEqual(
            detect_language("notepad open panni hello world nu type pannu"),
            "thanglish",
        )

    def test_english_planning_desktop_and_filesystem(self) -> None:
        self.assertEqual(
            self.planner.create_plan("Open Notepad").steps[0].tool_name,
            "desktop.notepad_open",
        )
        self.assertEqual(
            self.planner.create_plan("Open Windows Calculator").steps[0].tool_name,
            "desktop.calculator_open",
        )
        plan_np = self.planner.create_plan('open notepad and type "System Check Passed"')
        self.assertEqual(len(plan_np.steps), 1)
        self.assertEqual(plan_np.steps[0].tool_name, "desktop.notepad_write")
        self.assertEqual(plan_np.steps[0].parameters["text"], "System Check Passed")

        plan_calc = self.planner.create_plan("open calculator and calculate 45 * 2 + 10")
        self.assertEqual(len(plan_calc.steps), 1)
        self.assertEqual(plan_calc.steps[0].tool_name, "desktop.calculator_compute")
        self.assertEqual(plan_calc.steps[0].parameters["expression"], "45 * 2 + 10")

        for goal, expression in (
            ("calculate log 14", "log 14"),
            ("calculate sin 30", "sin 30"),
            ("open calculator and calculate 25*4", "25*4"),
        ):
            with self.subTest(goal=goal):
                plan = self.planner.create_plan(goal)
                self.assertEqual(plan.steps[0].tool_name, "desktop.calculator_compute")
                self.assertEqual(plan.steps[0].parameters["expression"], expression)

        radians_plan = self.planner.create_plan("calculate sin 1 radians")
        self.assertEqual(radians_plan.steps[0].parameters["angle_mode"], "radians")

        notepad_range = self.planner.create_plan("open Notepad and type numbers from 1 to 400")
        self.assertEqual(len(notepad_range.steps[0].parameters["text"].splitlines()), 400)
        self.assertEqual(notepad_range.steps[0].parameters["text"].splitlines()[0], "1")
        self.assertEqual(notepad_range.steps[0].parameters["text"].splitlines()[-1], "400")
        descending = self.planner.create_plan(
            "open Notepad and type numbers from 3 to 1 comma-separated"
        )
        self.assertEqual(descending.steps[0].parameters["text"], "3, 2, 1")

        plan_paint = self.planner.create_plan("launch paint and draw a box")
        self.assertEqual(plan_paint.steps[0].tool_name, "desktop.paint_launch")
        self.assertTrue(plan_paint.steps[0].parameters["draw_box"])

        circle_plan = self.planner.create_plan(
            "open Paint and draw a circle and save image in Downloads"
        )
        self.assertEqual(len(circle_plan.steps), 1)
        self.assertEqual(circle_plan.steps[0].tool_name, "desktop.paint_launch")
        self.assertEqual(circle_plan.steps[0].parameters["draw_shape"], "circle")
        self.assertEqual(
            circle_plan.steps[0].parameters["save_path"],
            "Downloads\\autocraft_drawing.png",
        )

        house_plan = self.planner.create_plan(
            "Draw a house in Paint and save it as house.png in Downloads"
        )
        self.assertEqual(house_plan.steps[0].tool_name, "desktop.paint_launch")
        self.assertEqual(house_plan.steps[0].parameters["draw_shape"], "house")
        self.assertEqual(
            house_plan.steps[0].parameters["save_path"],
            "Downloads\\house.png",
        )
        for goal, expected_shape in (
            ("Draw a triangle in Paint", "triangle"),
            ("Draw a simple landscape", "landscape"),
        ):
            with self.subTest(goal=goal):
                step = self.planner.create_plan(goal).steps[0]
                self.assertEqual(step.tool_name, "desktop.paint_launch")
                self.assertEqual(step.parameters["draw_shape"], expected_shape)

        mixed_plan = self.planner.create_plan(
            "open calculator and calculate 25*4 then open Paint and draw a circle"
        )
        self.assertEqual(
            [step.tool_name for step in mixed_plan.steps],
            ["desktop.calculator_compute", "desktop.paint_launch"],
        )
        self.assertEqual(mixed_plan.steps[1].depends_on, ["step_1"])

        plan_exp = self.planner.create_plan("open file explorer at reports")
        self.assertEqual(plan_exp.steps[0].tool_name, "desktop.explorer_open")
        self.assertEqual(plan_exp.steps[0].parameters["path"], "reports")

    def test_browser_navigation_keeps_requested_site_and_browser(self) -> None:
        cases = (
            (
                "open flipkart in chrome",
                "https://www.flipkart.com/",
                "chrome",
            ),
            (
                "Open Chrome and navigate to https://www.flipkart.com/",
                "https://www.flipkart.com/",
                "chrome",
            ),
            (
                "Open Microsoft Edge and navigate to https://www.wikipedia.org/",
                "https://www.wikipedia.org/",
                "msedge",
            ),
            ("open Amazon in Chrome", "https://www.amazon.com/", "chrome"),
            ("visit YouTube in Edge", "https://www.youtube.com/", "msedge"),
            ("open Python.org", "https://www.python.org/", None),
            (
                "navigate to https://www.iana.org/domains/reserved",
                "https://www.iana.org/domains/reserved",
                None,
            ),
        )

        for goal, expected_url, expected_browser in cases:
            with self.subTest(goal=goal):
                plan = self.planner.create_plan(goal)
                self.assertEqual(len(plan.steps), 1)
                step = plan.steps[0]
                self.assertEqual(step.tool_name, "browser.open_url")
                self.assertEqual(step.parameters["url"], expected_url)
                if expected_browser is not None:
                    self.assertEqual(step.parameters["browser"], expected_browser)
                else:
                    self.assertNotIn("browser", step.parameters)
                report = self.validator.validate(plan.steps)
                self.assertTrue(report.valid, report.errors)

    def test_notepad_generates_alphabet_and_sequence_formats(self) -> None:
        cases = (
            (
                "Open Notepad and type a to z",
                "abcdefghijklmnopqrstuvwxyz",
            ),
            ('Open Notepad and type "a to z"', "a to z"),
            (
                "Type a to z, each letter on a new line",
                "\n".join("abcdefghijklmnopqrstuvwxyz"),
            ),
            ("Type the alphabet in reverse", "zyxwvutsrqponmlkjihgfedcba"),
        )
        for goal, expected in cases:
            with self.subTest(goal=goal):
                step = self.planner.create_plan(goal).steps[0]
                self.assertEqual(step.tool_name, "desktop.notepad_write")
                self.assertEqual(step.parameters["text"], expected)

        numbers = self.planner.create_plan(
            "Type numbers from 1 to 100, each on a new line"
        ).steps[0]
        self.assertEqual(numbers.parameters["text"].splitlines(), [str(n) for n in range(1, 101)])

        saved = self.planner.create_plan(
            "Write the alphabet and save it as alphabet.txt in Downloads"
        ).steps[0]
        self.assertEqual(saved.parameters["text"], "abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(saved.parameters["save_path"], "Downloads\\alphabet.txt")

    def test_browser_url_normalization_and_filename_routing(self) -> None:
        cases = (
            ("open https://news.ycombinator.com/", "https://news.ycombinator.com/"),
            ("visit docs.python.org/3/", "https://docs.python.org/3/"),
            ("navigate to http://localhost:8000/status", "http://localhost:8000/status"),
        )
        for goal, expected_url in cases:
            with self.subTest(goal=goal):
                step = self.planner.create_plan(goal).steps[0]
                self.assertEqual(step.tool_name, "browser.open_url")
                self.assertEqual(step.parameters["url"], expected_url)
                self.assertEqual(step.max_retries, 0)

        paint = self.planner.create_plan(
            "Open Paint, draw a circle and save it as circle.png in Downloads"
        )
        self.assertTrue(self.validator.validate(paint.steps).valid)
        paint = paint.steps[0]
        self.assertEqual(paint.tool_name, "desktop.paint_launch")
        self.assertEqual(paint.parameters["draw_shape"], "circle")
        self.assertEqual(paint.parameters["save_path"], "Downloads\\circle.png")
        self.assertEqual(paint.max_retries, 0)

        inferred_paint = self.planner.create_plan(
            "Draw a circle and save it as circle.png in Downloads"
        )
        self.assertTrue(self.validator.validate(inferred_paint.steps).valid)
        inferred_paint = inferred_paint.steps[0]
        self.assertEqual(inferred_paint.tool_name, "desktop.paint_launch")
        self.assertEqual(inferred_paint.parameters["save_path"], "Downloads\\circle.png")

        launched_then_drawn = self.planner.create_plan(
            "Open Paint then draw a rectangle and save it in Downloads"
        ).steps[0]
        self.assertEqual(launched_then_drawn.tool_name, "desktop.paint_launch")
        self.assertEqual(launched_then_drawn.parameters["draw_shape"], "rectangle")

        notepad = self.planner.create_plan(
            'Open Notepad and type "circle.png"'
        ).steps[0]
        self.assertEqual(notepad.tool_name, "desktop.notepad_write")
        self.assertEqual(notepad.parameters["text"], "circle.png")

    def test_youtube_first_result_intent_uses_query_only_and_registered_tool(self) -> None:
        cases = (
            (
                "Search Tamil songs on YouTube",
                "Tamil songs",
                "search",
                1,
                None,
            ),
            (
                "Search Tamil songs on YouTube and play the first song",
                "Tamil songs",
                "play",
                1,
                None,
            ),
            (
                "Search Tamil songs on YouTube and play the second song",
                "Tamil songs",
                "play",
                2,
                None,
            ),
            (
                "Search Tamil songs on YouTube and play the third song",
                "Tamil songs",
                "play",
                3,
                None,
            ),
            (
                "Open YouTube in Chrome and search Tamil songs and play the first song",
                "Tamil songs",
                "play",
                1,
                "chrome",
            ),
            (
                "Search YouTube for Tamil comedy and play the first video",
                "Tamil comedy",
                "play",
                1,
                None,
            ),
            (
                "Search YouTube for AR Rahman songs and play the third result",
                "AR Rahman songs",
                "play",
                3,
                None,
            ),
            (
                "Search for Python tutorials and play the second video",
                "Python tutorials",
                "play",
                2,
                None,
            ),
            (
                "Search for Tamil comedy in YouTube",
                "Tamil comedy",
                "search",
                1,
                None,
            ),
            (
                "Open YouTube and search for 'Anirudh best songs'. Show the search results without intentionally selecting or playing any video.",
                "Anirudh best songs",
                "search",
                1,
                None,
            ),
            (
                "Open YouTube and search for Anirudh best songs. Show results without playing any video.",
                "Anirudh best songs",
                "search",
                1,
                None,
            ),
            (
                "Search YouTube for 'Play the first song' and show results.",
                "Play the first song",
                "search",
                1,
                None,
            ),
            (
                "Search for Tamil songs and play the second one",
                "Tamil songs",
                "play",
                2,
                None,
            ),
            (
                "Search YouTube for Tamil melody songs and play the 2nd video",
                "Tamil melody songs",
                "play",
                2,
                None,
            ),
            (
                "Search YouTube for Anirudh songs. Play the third result.",
                "Anirudh songs",
                "play",
                3,
                None,
            ),
        )
        for goal, query, action, position, browser in cases:
            with self.subTest(goal=goal):
                plan = self.planner.create_plan(goal)
                self.assertEqual(
                    [step.tool_name for step in plan.steps],
                    ["browser.open_url", "browser.youtube_first_result"],
                )
                self.assertEqual(plan.steps[1].parameters["query"], query)
                self.assertEqual(plan.steps[1].parameters["action"], action)
                self.assertEqual(plan.steps[1].parameters["position"], position)
                self.assertEqual(plan.steps[1].depends_on, ["step_1"])
                self.assertTrue(self.registry.has_tool(plan.steps[1].tool_name))
                if browser:
                    self.assertEqual(plan.steps[0].parameters["browser"], browser)
                    self.assertEqual(plan.steps[1].parameters["browser"], browser)
                self.assertTrue(self.validator.validate(plan.steps).valid)

    def test_youtube_ordinal_only_plans_use_existing_results(self) -> None:
        for goal, position in (
            ("Play the first song", 1),
            ("Play the 2nd video", 2),
            ("Play the third result", 3),
        ):
            with self.subTest(goal=goal):
                plan = self.planner.create_plan(goal)
                self.assertEqual(
                    [step.tool_name for step in plan.steps],
                    ["browser.youtube_first_result"],
                )
                self.assertEqual(plan.steps[0].parameters["query"], "")
                self.assertEqual(plan.steps[0].parameters["action"], "play")
                self.assertEqual(plan.steps[0].parameters["position"], position)
                self.assertTrue(self.validator.validate(plan.steps).valid)

    def test_browser_search_followed_by_click_remains_ordered(self) -> None:
        plan = self.planner.create_plan(
            'search browser for "Python tutorials" then click the first result'
        )
        self.assertEqual(
            [step.tool_name for step in plan.steps],
            ["browser.search", "browser.click"],
        )
        self.assertEqual(plan.steps[0].parameters["query"], "Python tutorials")
        self.assertEqual(plan.steps[1].depends_on, ["step_1"])

    def test_shopping_wishlist_workflow_extracts_only_product_query(self) -> None:
        cases = (
            (
                "Open Flipkart, search for Bluetooth speakers, and add a suitable product to my wishlist",
                "flipkart",
                "Bluetooth speakers",
            ),
            (
                "Open Flipkart and search for best rated speakers and add one to my wishlist",
                "flipkart",
                "best rated speakers",
            ),
            (
                "Search for best rated Bluetooth speakers on Amazon and add the selected product to my wishlist",
                "amazon",
                "best rated Bluetooth speakers",
            ),
            (
                "Open Flipkart, search for the best-rated speakers and add the best suitable product to my wishlist",
                "flipkart",
                "best-rated speakers",
            ),
            (
                "Open Amazon, search for the best-rated speakers and add the best suitable product to my wishlist",
                "amazon",
                "best-rated speakers",
            ),
        )
        for goal, site, query in cases:
            with self.subTest(goal=goal):
                plan = self.planner.create_plan(goal)
                self.assertEqual(
                    [step.tool_name for step in plan.steps],
                    ["browser.open_url", "browser.add_to_wishlist"],
                )
                self.assertEqual(plan.steps[1].parameters["site"], site)
                self.assertEqual(plan.steps[1].parameters["query"], query)
                self.assertEqual(plan.steps[1].depends_on, ["step_1"])
                self.assertTrue(self.registry.has_tool("browser.add_to_wishlist"))
                self.assertTrue(self.validator.validate(plan.steps).valid)

    def test_wikipedia_research_is_saved_from_browser_output_in_downloads(self) -> None:
        plan = self.planner.create_plan(
            "Search for the top 10 cybersecurity projects in Wikipedia, download the results and save them in Downloads."
        )
        self.assertEqual(
            [step.tool_name for step in plan.steps],
            [
                "browser.open_url",
                "browser.wikipedia_research",
                "fs.create_file",
            ],
        )
        self.assertEqual(plan.steps[1].parameters["query"], "cybersecurity projects")
        self.assertEqual(
            plan.steps[2].parameters["path"],
            "Downloads\\cybersecurity_research.txt",
        )
        self.assertEqual(
            plan.steps[2].parameters["content_from_step"],
            {"step_id": "step_2", "output_key": "summary"},
        )
        self.assertTrue(self.validator.validate(plan.steps).valid)

    def test_browser_intent_wins_over_application_names_in_search_and_urls(self) -> None:
        search = self.planner.create_plan("search browser for calculator")
        self.assertEqual(search.steps[0].tool_name, "browser.search")
        self.assertEqual(search.steps[0].parameters["query"], "calculator")

        read = self.planner.create_plan("read page about Paint in browser")
        self.assertEqual(read.steps[0].tool_name, "browser.read_content")

        navigation = self.planner.create_plan("open https://paint.net/")
        self.assertEqual(navigation.steps[0].tool_name, "browser.open_url")
        self.assertEqual(navigation.steps[0].parameters["url"], "https://paint.net/")

    def test_tamil_planning(self) -> None:
        plan_ta_np = self.planner.create_plan('நோட்பேட் திறந்து "வணக்கம் உலகம்" என்று எழுது')
        self.assertEqual(plan_ta_np.detected_language, "ta")
        self.assertEqual(plan_ta_np.steps[0].tool_name, "desktop.notepad_write")
        self.assertEqual(plan_ta_np.steps[0].parameters["text"], "வணக்கம் உலகம்")

        plan_ta_calc = self.planner.create_plan("கால்குலேட்டரில் 25 மற்றும் 75 கூட்டு")
        self.assertEqual(plan_ta_calc.detected_language, "ta")
        self.assertEqual(plan_ta_calc.steps[0].tool_name, "desktop.calculator_compute")
        self.assertEqual(plan_ta_calc.steps[0].parameters["expression"], "25 + 75")

        plan_ta_fs = self.planner.create_plan(
            'புதிய கோப்புறை docs உருவாக்கு பிறகு docs/note.txt கோப்பில் "தமிழ் குறிப்பு" என்று எழுது'
        )
        self.assertEqual(len(plan_ta_fs.steps), 2)
        self.assertEqual(plan_ta_fs.steps[0].tool_name, "fs.create_folder")
        self.assertEqual(plan_ta_fs.steps[1].tool_name, "fs.create_file")
        self.assertEqual(plan_ta_fs.steps[1].depends_on, ["step_1"])

    def test_thanglish_planning(self) -> None:
        plan_tg_np = self.planner.create_plan(
            'notepad open panni "Vanakkam Thanglish" nu type pannu'
        )
        self.assertEqual(plan_tg_np.detected_language, "thanglish")
        self.assertEqual(plan_tg_np.steps[0].tool_name, "desktop.notepad_write")
        self.assertEqual(plan_tg_np.steps[0].parameters["text"], "Vanakkam Thanglish")

        plan_tg_calc = self.planner.create_plan(
            "calculator open panni 15 + 85 calculate pannu"
        )
        self.assertEqual(plan_tg_calc.steps[0].tool_name, "desktop.calculator_compute")
        self.assertIn("15 + 85", plan_tg_calc.steps[0].parameters["expression"])

        plan_tg_del = self.planner.create_plan("old_data.txt file delete pannu")
        self.assertEqual(plan_tg_del.steps[0].tool_name, "fs.delete_path")
        self.assertTrue(plan_tg_del.steps[0].requires_approval)

    def test_multi_step_dependencies(self) -> None:
        goal = (
            'create folder build_logs and then create file build_logs/run.txt with content "OK" '
            "and then copy build_logs/run.txt to build_logs/run_backup.txt "
            "and then read file build_logs/run_backup.txt"
        )
        plan = self.planner.create_plan(goal)
        self.assertEqual(len(plan.steps), 4)
        self.assertEqual(plan.steps[0].depends_on, [])
        self.assertEqual(plan.steps[1].depends_on, ["step_1"])
        self.assertEqual(plan.steps[2].depends_on, ["step_2"])
        self.assertEqual(plan.steps[3].depends_on, ["step_3"])

        report = self.validator.validate(plan.steps)
        self.assertTrue(report.valid, f"Expected valid plan, got errors: {report.errors}")

    def test_empty_and_unsupported_goals_rejected(self) -> None:
        with self.assertRaises(PlanningError):
            self.planner.create_plan("")
        with self.assertRaises(PlanningError):
            self.planner.create_plan("   ")
        with self.assertRaises(PlanningError):
            self.planner.create_plan("teleport my coffee mug to mars")

    def test_plan_validator_rejects_empty_and_malformed_plans(self) -> None:
        # 1. Empty plan list
        rep_empty = self.validator.validate([])
        self.assertFalse(rep_empty.valid)
        self.assertIn("empty", rep_empty.errors[0].lower())

        # 2. Unregistered tool
        bad_tool_step = PlanStep(
            step_id="step_1",
            step_order=1,
            title="Fake tool",
            tool_name="quantum.teleport",
            parameters={},
        )
        rep_unreg = self.validator.validate([bad_tool_step])
        self.assertFalse(rep_unreg.valid)
        self.assertTrue(any("unregistered tool" in e for e in rep_unreg.errors))

        # 3. Invalid JSON schema arguments (missing required 'content' for fs.create_file)
        bad_schema_step = PlanStep(
            step_id="step_1",
            step_order=1,
            title="Missing content",
            tool_name="fs.create_file",
            parameters={"path": "test.txt"},
        )
        rep_schema = self.validator.validate([bad_schema_step])
        self.assertFalse(rep_schema.valid)
        self.assertTrue(any("schema validation" in e for e in rep_schema.errors))

        # 4. Circular dependency cycle (step_1 -> step_2 -> step_1)
        s1 = PlanStep(
            step_id="step_1",
            step_order=1,
            title="Folder 1",
            tool_name="fs.create_folder",
            parameters={"path": "a"},
            depends_on=["step_2"],
        )
        s2 = PlanStep(
            step_id="step_2",
            step_order=2,
            title="Folder 2",
            tool_name="fs.create_folder",
            parameters={"path": "b"},
            depends_on=["step_1"],
        )
        rep_cycle = self.validator.validate([s1, s2])
        self.assertFalse(rep_cycle.valid)
        self.assertTrue(any("circular dependency" in e.lower() for e in rep_cycle.errors))

        # 5. Path traversal outside workspace blocked during validation
        traversal_step = PlanStep(
            step_id="step_1",
            step_order=1,
            title="Escape sandbox",
            tool_name="fs.create_file",
            parameters={"path": "../../etc/passwd", "content": "hack"},
        )
        rep_sec = self.validator.validate([traversal_step])
        self.assertFalse(rep_sec.valid)
        self.assertTrue(any("blocked by security policy" in e.lower() for e in rep_sec.errors))


if __name__ == "__main__":
    unittest.main()
