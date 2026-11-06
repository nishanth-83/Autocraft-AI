/**
 * Frontend Unit & Component Tests for AUTOCRAFT AI Dashboard.
 * Tests verified progress computation, API client route consistency,
 * multilingual presets, and App VDOM rendering.
 */

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  App,
  computeVerifiedProgress,
  verifiedCompletionMessage,
  EXAMPLE_GOALS,
} from "../App";
import { AutocraftApi } from "../api";
import { TaskStep } from "../types";

function assert(condition: boolean, message: string): void {
  if (!condition) {
    throw new Error(`Assertion failed: ${message}`);
  }
}

export async function runFrontendTests(): Promise<{ passed: number; failed: number; results: string[] }> {
  const results: string[] = [];
  let passed = 0;
  let failed = 0;

  async function test(name: string, fn: () => void | Promise<void>) {
    try {
      await fn();
      passed += 1;
      results.push(`PASS: ${name}`);
    } catch (err: unknown) {
      failed += 1;
      const msg = err instanceof Error ? err.message : String(err);
      results.push(`FAIL: ${name} -> ${msg}`);
    }
  }

  await test("computeVerifiedProgress returns 0 for empty or unverified steps (never fabricates progress)", () => {
    assert(computeVerifiedProgress([]) === 0, "Empty steps should be 0%");

    const unverifiedSteps: TaskStep[] = [
      {
        step_id: "step_1",
        step_order: 1,
        title: "Create file",
        tool_name: "fs.create_file",
        parameters: { path: "a.txt", content: "hi" },
        depends_on: [],
        expected_condition: "exists",
        requires_approval: false,
        status: "COMPLETED",
        attempt_count: 1,
        max_retries: 2,
        execution_output: {},
        verification: { verified: false, summary: "Unverified" },
        error: {},
      },
    ];
    assert(
      computeVerifiedProgress(unverifiedSteps) === 0,
      "COMPLETED step without verification.verified=true must be 0%"
    );
  });

  await test("computeVerifiedProgress accurately computes percentage for verified steps", () => {
    const steps: TaskStep[] = [
      {
        step_id: "step_1",
        step_order: 1,
        title: "Create folder",
        tool_name: "fs.create_folder",
        parameters: { path: "dir" },
        depends_on: [],
        expected_condition: "exists",
        requires_approval: false,
        status: "COMPLETED",
        attempt_count: 1,
        max_retries: 2,
        execution_output: {},
        verification: { verified: true, summary: "Verified folder" },
        error: {},
      },
      {
        step_id: "step_2",
        step_order: 2,
        title: "Create file",
        tool_name: "fs.create_file",
        parameters: { path: "dir/a.txt", content: "123" },
        depends_on: ["step_1"],
        expected_condition: "exists",
        requires_approval: false,
        status: "RUNNING",
        attempt_count: 1,
        max_retries: 2,
        execution_output: {},
        verification: {},
        error: {},
      },
    ];
    assert(computeVerifiedProgress(steps) === 50, "1 of 2 verified steps should equal 50%");
    assert(
      verifiedCompletionMessage(steps) === null,
      "A partially verified task must not be reported as completed"
    );
    steps[1].status = "COMPLETED";
    steps[1].verification = { verified: true, summary: "Verified file" };
    assert(
      verifiedCompletionMessage(steps) === "Task completed with 100% verified progress.",
      "Completion requires verified evidence for every step"
    );
  });

  await test("EXAMPLE_GOALS covers English, Tamil, and Thanglish presets", () => {
    const langs = new Set(EXAMPLE_GOALS.map((g) => g.lang));
    assert(langs.has("EN"), "Must include English preset");
    assert(langs.has("TA"), "Must include Tamil preset");
    assert(langs.has("TG"), "Must include Thanglish preset");
  });

  await test("task input renders as a normal enabled text field", () => {
    const markup = renderToStaticMarkup(React.createElement(App));
    const input = markup.match(/<textarea\b[^>]*>/)?.[0] || "";
    assert(input.length > 0, "Task input must be rendered");
    assert(input.includes('aria-label="Mission goal"'), "Task input must have an accessible label");
    assert(!/\sdisabled(?:\s|=|>)/.test(input), "Task input must remain enabled during tasks");
    assert(!/oncontextmenu/i.test(input), "Editing must not depend on right-click");
    assert(EXAMPLE_GOALS.some((goal) => goal.goal.includes("flipkart.com")), "Browser preset must use a requested real site");
  });

  await test("AutocraftApi client routes match FastAPI backend routes", async () => {
    const calledUrls: Array<{ url: string; method: string }> = [];
    const origFetch = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      calledUrls.push({
        url: String(input),
        method: (init?.method || "GET").toUpperCase(),
      });
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: "healthy", tasks: [], approvals: [], tools: [] }),
      } as Response;
    }) as typeof fetch;

    try {
      await AutocraftApi.getHealth();
      await AutocraftApi.getObservation();
      await AutocraftApi.listTools();
      await AutocraftApi.previewPlan("open paint");
      await AutocraftApi.startTask("create folder test", false);
      await AutocraftApi.cancelTask("task_123");
      await AutocraftApi.resolveApproval("appr_1", true);
      await AutocraftApi.emergencyStop();

      const routes = calledUrls.map((c) => `${c.method} ${c.url}`);
      assert(routes.includes("GET /api/health"), "Health route mismatch");
      assert(routes.includes("GET /api/observe"), "Observe route mismatch");
      assert(routes.includes("GET /api/tools"), "Tools route mismatch");
      assert(routes.includes("POST /api/plan/preview"), "Plan preview route mismatch");
      assert(routes.includes("POST /api/tasks"), "Start task route mismatch");
      assert(routes.includes("POST /api/tasks/task_123/cancel"), "Cancel task route mismatch");
      assert(routes.includes("POST /api/approvals/appr_1/resolve"), "Resolve approval route mismatch");
      assert(routes.includes("POST /api/emergency-stop"), "Emergency stop route mismatch");
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  await test("App component renders complete dashboard VDOM structure with controls and telemetry panels", () => {
    const markup = renderToStaticMarkup(React.createElement(App));
    assert(markup.includes("AUTOCRAFT AI"), "Header title must render");
    assert(markup.includes("health-strip"), "Health strip must render");
    assert(markup.includes("goal-input"), "Goal input textarea must render");
    assert(markup.includes("start-btn"), "Start button must render");
    assert(markup.includes("stop-btn"), "Stop button must render");
    assert(markup.includes("clear-btn"), "Clear button must render");
    assert(markup.includes("emergency-stop-btn"), "Emergency Stop button must render");
    assert(markup.includes("loop-pipeline"), "Execution loop pipeline must render");
    assert(markup.includes("observation-panel"), "Observation panel must render");
    assert(markup.includes("task-history-panel"), "Task history panel must render");
  });

  return { passed, failed, results };
}
