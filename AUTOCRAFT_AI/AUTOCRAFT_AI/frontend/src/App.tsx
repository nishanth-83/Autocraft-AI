/**
 * AUTOCRAFT AI — Autonomous Desktop Agent Dashboard (React + TypeScript).
 * Displays real backend state:
 * - Task input (English, Tamil, Thanglish presets)
 * - Autonomous Loop Phase Indicator (OBSERVE -> PLAN -> VALIDATE -> ACT -> VERIFY -> RECOVER)
 * - Current Observation & System/Automation Health
 * - Generated Plan, Executable Steps, Selected Tools, and Current Action
 * - Real Execution Progress & Independent Verification Evidence
 * - Errors & Recovery Attempts
 * - Security Approval Queue (Authorize / Deny)
 * - Task History & Start / Stop / Emergency Stop / Clear Controls
 */

import React, { useState, useEffect, useRef } from "react";
import { AutocraftApi } from "./api";
import {
  ApprovalRequest,
  EnvironmentObservation,
  ExecutionPhase,
  HealthData,
  PlanPreviewResponse,
  TaskRecord,
  TaskStep,
  ToolMetadata,
} from "./types";

const LOOP_PHASES: ExecutionPhase[] = [
  "OBSERVE",
  "PLAN",
  "VALIDATE",
  "ACT",
  "VERIFY",
  "RECOVER",
];

export const EXAMPLE_GOALS = [
  {
    label: "English: Multi-Step File Workflow",
    lang: "EN",
    goal: 'create folder project_logs and then create file project_logs/status.txt with content "AUTOCRAFT ONLINE" and then read file project_logs/status.txt',
  },
  {
    label: "Tamil: கோப்புறை & கோப்பு உருவாக்கு",
    lang: "TA",
    goal: 'புதிய கோப்புறை tamil_notes உருவாக்கு பிறகு tamil_notes/vanakkam.txt கோப்பில் "வணக்கம் ஆட்டோகிராப்ட்" என்று எழுது',
  },
  {
    label: "Thanglish: Notepad Automation",
    lang: "TG",
    goal: 'notepad open panni "Vanakkam from AUTOCRAFT AI" nu type pannu',
  },
  {
    label: "English: Calculator Computation",
    lang: "EN",
    goal: "open calculator and calculate 125 + 375",
  },
  {
    label: "English: Destructive File Delete (Approval Gate)",
    lang: "EN",
    goal: 'create file temp_cleanup.txt with content "temporary data" and then delete file temp_cleanup.txt',
  },
  {
    label: "Browser: Flipkart navigation",
    lang: "EN",
    goal: "Open Chrome and navigate to https://www.flipkart.com/",
  },
];

export function computeVerifiedProgress(steps: TaskStep[]): number {
  if (!steps || steps.length === 0) {
    return 0;
  }
  const verifiedCompleted = steps.filter(
    (s) => s.status === "COMPLETED" && s.verification && s.verification.verified === true
  ).length;
  return Math.round((verifiedCompleted / steps.length) * 100);
}

export function verifiedCompletionMessage(steps: TaskStep[]): string | null {
  return computeVerifiedProgress(steps) === 100
    ? "Task completed with 100% verified progress."
    : null;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function App(): React.ReactElement {
  const [goalInput, setGoalInput] = useState<string>("");
  const [health, setHealth] = useState<HealthData | null>(null);
  const [observation, setObservation] = useState<EnvironmentObservation | null>(null);
  const [tools, setTools] = useState<ToolMetadata[]>([]);
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [activeTask, setActiveTask] = useState<TaskRecord | null>(null);
  const [planPreview, setPlanPreview] = useState<PlanPreviewResponse | null>(null);
  const [pendingApprovals, setPendingApprovals] = useState<ApprovalRequest[]>([]);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [submittingAction, setSubmittingAction] = useState<"task" | "preview" | "approval" | null>(null);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [successBanner, setSuccessBanner] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"steps" | "verification" | "recovery" | "tools">("steps");
  const goalInputRef = useRef<HTMLTextAreaElement>(null);

  const focusGoalInput = () => {
    window.requestAnimationFrame(() => goalInputRef.current?.focus({ preventScroll: true }));
  };

  const refreshSystemState = async (selectedTaskId?: string): Promise<TaskRecord | null> => {
    const results = await Promise.allSettled([
      AutocraftApi.getHealth(),
      AutocraftApi.getObservation(),
      AutocraftApi.listTasks(30),
      AutocraftApi.listPendingApprovals(),
      AutocraftApi.listTools(),
    ]);
    const [healthResult, observationResult, tasksResult, approvalsResult, toolsResult] = results;
    const failures = results
      .filter((result): result is PromiseRejectedResult => result.status === "rejected")
      .map((result) => result.reason instanceof Error ? result.reason.message : String(result.reason));

    if (healthResult.status === "fulfilled") {
      setHealth(healthResult.value);
    } else {
      setHealth(null);
    }
    if (observationResult.status === "fulfilled") {
      setObservation(observationResult.value);
    } else {
      setObservation(null);
    }
    if (tasksResult.status === "fulfilled") {
      setTasks(tasksResult.value.tasks);
    }
    if (approvalsResult.status === "fulfilled") {
      setPendingApprovals(approvalsResult.value.approvals);
    }
    if (toolsResult.status === "fulfilled") {
      setTools(toolsResult.value.tools);
    }
    if (failures.length > 0) {
      setErrorBanner(`Backend request warning: ${failures.join("; ")}`);
    }

    if (tasksResult.status !== "fulfilled") {
      return null;
    }
    const targetId =
      selectedTaskId || (activeTask ? activeTask.task_id : tasksResult.value.tasks[0]?.task_id);
    if (targetId) {
      const found = tasksResult.value.tasks.find((task) => task.task_id === targetId);
      if (found) {
        setActiveTask(found);
        return found;
      }
    }
    return null;
  };

  useEffect(() => {
    refreshSystemState();
  }, []);

  useEffect(() => {
    const taskId = activeTask?.task_id;
    const taskStatus = activeTask?.status;
    if (!taskId || !taskStatus || ["COMPLETED", "FAILED", "CANCELLED"].includes(taskStatus)) {
      return;
    }

    let disposed = false;
    let timer: number;
    const pollTask = async () => {
      try {
        const latest = await AutocraftApi.getTask(taskId);
        if (disposed) return;
        setActiveTask(latest);
        if (latest.status === "COMPLETED") {
          const completionMessage = verifiedCompletionMessage(latest.steps);
          if (completionMessage) {
            setSuccessBanner(completionMessage);
          } else {
            setSuccessBanner(null);
            setErrorBanner(
              `Task was marked COMPLETED, but only ${computeVerifiedProgress(latest.steps)}% of its steps have verified evidence.`
            );
          }
        } else if (latest.status === "FAILED") {
          setErrorBanner(latest.error_summary || "Task failed. Review its execution details.");
        } else if (latest.status === "CANCELLED") {
          setSuccessBanner(latest.error_summary || "Task cancelled.");
        }
        if (!["COMPLETED", "FAILED", "CANCELLED"].includes(latest.status)) {
          timer = window.setTimeout(pollTask, 700);
        }
      } catch (err: unknown) {
        if (disposed) return;
        setErrorBanner(err instanceof Error ? err.message : String(err));
        timer = window.setTimeout(pollTask, 2500);
      }
    };

    timer = window.setTimeout(pollTask, 350);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [activeTask?.task_id, activeTask?.status]);

  const handlePreviewPlan = async () => {
    setErrorBanner(null);
    setSuccessBanner(null);
    setIsSubmitting(true);
    setSubmittingAction("preview");
    try {
      const preview = await AutocraftApi.previewPlan(goalInput);
      setPlanPreview(preview);
      setSuccessBanner(
        preview.validation.valid
          ? `Plan ready: ${preview.plan.steps.length} executable step(s).`
          : `Plan generated with validation issues: ${preview.validation.errors.join("; ")}`
      );
    } catch (err: unknown) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSubmitting(false);
      setSubmittingAction(null);
      focusGoalInput();
    }
  };

  const handleStartTask = async () => {
    setErrorBanner(null);
    setSuccessBanner(null);
    setPlanPreview(null);
    setIsSubmitting(true);
    setSubmittingAction("task");
    try {
      const created = await AutocraftApi.startTask(goalInput, true);
      setActiveTask(created);
      setSuccessBanner("Task started. Execution progress will update automatically.");
      const latest = await refreshSystemState(created.task_id);
      if (latest?.status === "COMPLETED") {
        const completionMessage = verifiedCompletionMessage(latest.steps);
        if (completionMessage) {
          setSuccessBanner(completionMessage);
        } else {
          setSuccessBanner(null);
          setErrorBanner(
            `Task was marked COMPLETED, but only ${computeVerifiedProgress(latest.steps)}% of its steps have verified evidence.`
          );
        }
      } else if (latest?.status === "FAILED") {
        setErrorBanner(latest.error_summary || "Task failed. Review its execution details.");
      } else if (latest?.status === "CANCELLED") {
        setSuccessBanner(latest.error_summary || "Task cancelled.");
      }
    } catch (err: unknown) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSubmitting(false);
      setSubmittingAction(null);
      focusGoalInput();
    }
  };

  const handleStopTask = async () => {
    if (!activeTask) return;
    setErrorBanner(null);
    setSuccessBanner(null);
    try {
      const updated = await AutocraftApi.cancelTask(
        activeTask.task_id,
        "Stopped by operator via Stop control."
      );
      setActiveTask(updated);
      await refreshSystemState(updated.task_id);
    } catch (err: unknown) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      focusGoalInput();
    }
  };

  const handleEmergencyStop = async () => {
    setErrorBanner(null);
    setSuccessBanner(null);
    try {
      await AutocraftApi.emergencyStop();
      await refreshSystemState();
      setSuccessBanner("Emergency Stop is active. New task execution is blocked.");
    } catch (err: unknown) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      focusGoalInput();
    }
  };

  const handleResetEmergencyStop = async () => {
    setErrorBanner(null);
    setSuccessBanner(null);
    try {
      await AutocraftApi.resetEmergencyStop();
      await refreshSystemState();
      setSuccessBanner("Emergency Stop reset. Tasks can be started again.");
    } catch (err: unknown) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      focusGoalInput();
    }
  };

  const handleClearView = async () => {
    setPlanPreview(null);
    setErrorBanner(null);
    setSuccessBanner(null);
    try {
      const result = await AutocraftApi.clearHistory();
      setActiveTask(null);
      await refreshSystemState();
      setSuccessBanner(`Cleared ${result.deleted_tasks} task(s) from history.`);
    } catch (err: unknown) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      focusGoalInput();
    }
  };

  const handleResolveApproval = async (approvalId: string, approved: boolean) => {
    setErrorBanner(null);
    setIsSubmitting(true);
    setSubmittingAction("approval");
    try {
      const res = await AutocraftApi.resolveApproval(
        approvalId,
        approved,
        approved ? "Approved by operator in dashboard" : "Rejected by operator in dashboard"
      );
      setActiveTask(res.task);
      await refreshSystemState(res.task.task_id);
    } catch (err: unknown) {
      setErrorBanner(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSubmitting(false);
      setSubmittingAction(null);
    }
  };

  const displayedSteps: TaskStep[] = activeTask
    ? activeTask.steps
    : planPreview
    ? planPreview.plan.steps
    : [];
  const taskIsRunning = Boolean(
    activeTask && !["COMPLETED", "FAILED", "CANCELLED"].includes(activeTask.status)
  );

  const progressPct = activeTask ? computeVerifiedProgress(activeTask.steps) : 0;
  const activePhase: ExecutionPhase = activeTask ? activeTask.current_phase : "IDLE";
  const currentObservation: EnvironmentObservation | null =
    (activeTask && activeTask.observation && Object.keys(activeTask.observation).length > 0
      ? activeTask.observation
      : observation) || null;

  const recoveryAndErrorLogs = activeTask
    ? activeTask.logs.filter((l) =>
        ["ERROR", "WARN", "RECOVERY", "SECURITY"].includes(l.level)
      )
    : [];

  return (
    <div className="ac-shell" data-testid="autocraft-dashboard">
      {/* Top Header & System Health */}
      <header className="ac-header">
        <div className="ac-brand">
          <div className="ac-orb" />
          <div>
            <h1>AUTOCRAFT AI</h1>
            <p className="ac-subtitle">
              Autonomous Desktop Agent • Deterministic Offline Planner • Verified Execution Loop
            </p>
          </div>
        </div>

        <div className="ac-health-strip" data-testid="health-strip">
          <span className={`ac-pill ${health?.status === "healthy" ? "ok" : "warn"}`}>
            Backend: {health?.status || "connecting..."}
          </span>
          <span
            className={`ac-pill ${health?.database?.healthy ? "ok" : "err"}`}
            title={health?.database?.error || undefined}
          >
            SQLite: {health?.database?.healthy ? "Online" : "Offline"}
          </span>
          <span
            className={`ac-pill ${health?.browser_available ? "ok" : "warn"}`}
            title={health?.browser_error || undefined}
          >
            Browser ({health?.browser_engine || "none"}):{" "}
            {health?.browser_available ? "Installed" : "Unavailable"}
          </span>
          <span
            className={`ac-pill ${health?.desktop_automation_supported ? "ok" : "neutral"}`}
            title={health?.desktop_error || undefined}
          >
            Windows UIA ({health?.platform || "unknown"}):{" "}
            {health?.desktop_automation_supported ? "Live" : "Unavailable"}
          </span>
          <span className="ac-pill neutral">
            Tools: {health?.registered_tool_count ?? 0}
          </span>
        </div>
      </header>

      {/* Emergency Stop Banner if active */}
      {health?.emergency_stop_active && (
        <div className="ac-emergency-banner" data-testid="emergency-banner">
          <strong>EMERGENCY STOP ACTIVE:</strong> All task execution and browser automation have been halted.
          <button className="ac-btn ac-btn-secondary" onClick={handleResetEmergencyStop}>
            Reset Emergency Stop
          </button>
        </div>
      )}

      {errorBanner && (
        <div className="ac-error-banner" data-testid="error-banner">
          <span>{errorBanner}</span>
          <button className="ac-btn-close" onClick={() => setErrorBanner(null)}>
            ×
          </button>
        </div>
      )}

      {successBanner && (
        <div className="ac-success-banner" role="status" data-testid="success-banner">
          <span>{successBanner}</span>
          <button className="ac-btn-close" onClick={() => setSuccessBanner(null)} aria-label="Dismiss notification">
            ×
          </button>
        </div>
      )}

      {/* Task Input & Primary Controls */}
      <section className="ac-command-card">
        <div className="ac-command-header">
          <label htmlFor="goal-input" className="ac-section-title">
            Mission goal <span className="ac-input-hint">English · தமிழ் · Thanglish</span>
          </label>
          <div className="ac-preset-chips">
            {EXAMPLE_GOALS.map((ex, idx) => (
              <button
                key={idx}
                type="button"
                className="ac-chip"
                onClick={() => setGoalInput(ex.goal)}
              >
                <span className="ac-chip-lang">{ex.lang}</span> {ex.label}
              </button>
            ))}
          </div>
        </div>

        <div className="ac-input-row">
          <textarea
            id="goal-input"
            data-testid="goal-input"
            ref={goalInputRef}
            className="ac-goal-textarea"
            rows={2}
            value={goalInput}
            onChange={(e) => setGoalInput(e.target.value)}
            aria-label="Mission goal"
            placeholder="What would you like AUTOCRAFT AI to do?"
          />
          <div className="ac-control-buttons">
            <button
              type="button"
              data-testid="start-btn"
              className="ac-btn ac-btn-primary"
              disabled={isSubmitting || taskIsRunning || !goalInput.trim() || health?.emergency_stop_active}
              onClick={handleStartTask}
            >
              {submittingAction === "task" ? <span><span className="ac-spinner" /> Starting...</span> : "▶ Start Task"}
            </button>
            <button
              type="button"
              data-testid="preview-btn"
              className="ac-btn ac-btn-secondary"
              disabled={isSubmitting || !goalInput.trim()}
              onClick={handlePreviewPlan}
            >
              {submittingAction === "preview" ? <span><span className="ac-spinner" /> Planning...</span> : "Plan Preview"}
            </button>
            <button
              type="button"
              data-testid="stop-btn"
              className="ac-btn ac-btn-warn"
              disabled={!taskIsRunning}
              onClick={handleStopTask}
            >
              ■ Stop
            </button>
            <button
              type="button"
              data-testid="clear-btn"
              className="ac-btn ac-btn-ghost"
              disabled={taskIsRunning}
              onClick={handleClearView}
            >
              Clear History
            </button>
            <button
              type="button"
              data-testid="emergency-stop-btn"
              className="ac-btn ac-btn-danger"
              disabled={health?.emergency_stop_active}
              onClick={handleEmergencyStop}
            >
              ⚠ Emergency Stop
            </button>
          </div>
        </div>
      </section>

      {/* Autonomous Execution Loop Pipeline Bar */}
      <section className="ac-pipeline-bar" data-testid="loop-pipeline">
        <div className="ac-pipeline-steps">
          {LOOP_PHASES.map((phase) => {
            const isCurrent = activePhase === phase;
            return (
              <div
                key={phase}
                className={`ac-phase-node ${isCurrent ? "active" : ""}`}
              >
                <span className="ac-phase-dot" />
                <span className="ac-phase-name">{phase}</span>
              </div>
            );
          })}
        </div>
        <div className="ac-progress-summary">
          <div className="ac-progress-meta">
            <span>
              Status:{" "}
              <strong data-testid="active-task-status">
                {activeTask ? activeTask.status : planPreview ? "PLAN_PREVIEW" : "IDLE"}
              </strong>
            </span>
            <span>
              Verified Progress: <strong data-testid="verified-progress">{progressPct}%</strong>
            </span>
          </div>
          <div className="ac-progress-track">
            <div
              className="ac-progress-fill"
              style={{ width: `${progressPct}%` }}
            />
          </div>
        </div>
      </section>

      {/* Pending Security Approvals Alert Panel */}
      {pendingApprovals.length > 0 && (
        <section className="ac-approvals-panel" data-testid="approvals-panel">
          <h3>🔒 Security Approval Required ({pendingApprovals.length})</h3>
          <div className="ac-approval-list">
            {pendingApprovals.map((appr) => (
              <div key={appr.approval_id} className="ac-approval-card">
                <div>
                  <span className="ac-badge risk">{appr.risk_level} RISK</span>
                  <strong>{appr.action_summary}</strong>
                  <p className="ac-approval-reason">{appr.reason}</p>
                  <code className="ac-code-inline">
                    Tool: {appr.tool_name} | Params: {JSON.stringify(appr.parameters)}
                  </code>
                </div>
                <div className="ac-approval-actions">
                  <button
                    type="button"
                    data-testid={`approve-${appr.approval_id}`}
                    className="ac-btn ac-btn-primary"
                    onClick={() => handleResolveApproval(appr.approval_id, true)}
                  >
                    ✓ Authorize Action
                  </button>
                  <button
                    type="button"
                    data-testid={`reject-${appr.approval_id}`}
                    className="ac-btn ac-btn-danger"
                    onClick={() => handleResolveApproval(appr.approval_id, false)}
                  >
                    ✕ Deny Action
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Main 3-Column Workspace Grid */}
      <div className="ac-main-grid">
        {/* Left Column: Current Observation & Task History */}
        <aside className="ac-col-sidebar">
          <div className="ac-card" data-testid="observation-panel">
            <h3>Current Observation</h3>
            {currentObservation ? (
              <ul className="ac-kv-list">
                <li>
                  <span>OS Platform:</span>
                  <strong>{currentObservation.os_release || currentObservation.platform}</strong>
                </li>
                <li>
                  <span>Desktop UIA:</span>
                  <strong>
                    {currentObservation.desktop_automation_supported
                      ? `Supported (${currentObservation.open_windows_count ?? 0} windows)`
                      : "Disabled (Non-Windows OS)"}
                  </strong>
                </li>
                <li>
                  <span>Browser Engine:</span>
                  <strong>
                    {currentObservation.browser_engine} (
                    {currentObservation.browser_available ? "Installed" : "Unavailable"})
                  </strong>
                </li>
                <li>
                  <span>Workspace Root:</span>
                  <code className="ac-code-small">{currentObservation.workspace_root}</code>
                </li>
                <li>
                  <span>Workspace Entries ({currentObservation.workspace_entries?.length ?? 0}):</span>
                  <div className="ac-tag-cloud">
                    {(currentObservation.workspace_entries || []).length === 0 ? (
                      <span className="ac-muted">Empty workspace</span>
                    ) : (
                      (currentObservation.workspace_entries || []).map((entry, idx) => (
                        <span key={idx} className="ac-file-tag">
                          {entry}
                        </span>
                      ))
                    )}
                  </div>
                </li>
              </ul>
            ) : (
              <p className="ac-muted">Collecting environment observation...</p>
            )}
          </div>

          <div className="ac-card" data-testid="task-history-panel">
            <div className="ac-card-title-row">
              <h3>Task History ({tasks.length})</h3>
              <button
                type="button"
                className="ac-btn-mini"
                onClick={() => refreshSystemState()}
              >
                ↻ Refresh
              </button>
            </div>
            <div className="ac-history-list">
              {tasks.length === 0 ? (
                <p className="ac-muted">No tasks executed yet.</p>
              ) : (
                tasks.map((t) => (
                  <div
                    key={t.task_id}
                    className={`ac-history-item ${
                      activeTask?.task_id === t.task_id ? "selected" : ""
                    }`}
                    role="button"
                    tabIndex={0}
                    aria-pressed={activeTask?.task_id === t.task_id}
                    onClick={() => {
                      setPlanPreview(null);
                      setActiveTask(t);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setPlanPreview(null);
                        setActiveTask(t);
                      }
                    }}
                  >
                    <div className="ac-history-top">
                      <span className={`ac-status-badge status-${t.status.toLowerCase()}`}>
                        {t.status}
                      </span>
                      <span className="ac-lang-badge">{t.detected_language.toUpperCase()}</span>
                    </div>
                    <p className="ac-history-goal">{t.goal}</p>
                    <span className="ac-history-meta">
                      {t.steps.length} step(s) • {formatTimestamp(t.created_at)}
                    </span>
                  </div>
                ))
              )}
            </div>
          </div>
        </aside>

        {/* Center/Right Column: Plan, Steps, Verification Results, Errors/Recovery, Tools */}
        <main className="ac-col-main">
          <div className="ac-card">
            <div className="ac-tabs">
              <button
                type="button"
                className={`ac-tab ${activeTab === "steps" ? "active" : ""}`}
                onClick={() => setActiveTab("steps")}
              >
                Generated Plan & Executable Steps ({displayedSteps.length})
              </button>
              <button
                type="button"
                className={`ac-tab ${activeTab === "verification" ? "active" : ""}`}
                onClick={() => setActiveTab("verification")}
              >
                Verification Evidence
              </button>
              <button
                type="button"
                className={`ac-tab ${activeTab === "recovery" ? "active" : ""}`}
                onClick={() => setActiveTab("recovery")}
              >
                Errors & Recovery ({recoveryAndErrorLogs.length})
              </button>
              <button
                type="button"
                className={`ac-tab ${activeTab === "tools" ? "active" : ""}`}
                onClick={() => setActiveTab("tools")}
              >
                Registered Tools ({tools.length})
              </button>
            </div>

            {/* Active Task Summary Banner */}
            {activeTask && (
              <div className="ac-task-summary-bar" data-testid="active-task-banner">
                <div>
                  <strong>Goal:</strong> {activeTask.goal}{" "}
                  <span className="ac-lang-badge">
                    {activeTask.detected_language.toUpperCase()}
                  </span>
                </div>
                <div>
                  <strong>Current Action:</strong>{" "}
                  <code>{activeTask.current_step_id || "None (Idle)"}</code>
                </div>
              </div>
            )}

            {/* TAB 1: Executable Steps */}
            {activeTab === "steps" && (
              <div className="ac-steps-container" data-testid="steps-container">
                {displayedSteps.length === 0 ? (
                  <p className="ac-muted">
                    Enter a goal above and click <strong>Start Task</strong> or{" "}
                    <strong>Plan Preview</strong> to generate an ordered executable plan.
                  </p>
                ) : (
                  displayedSteps.map((step) => (
                    <div
                      key={step.step_id}
                      className={`ac-step-card status-border-${step.status.toLowerCase()}`}
                      data-testid={`step-card-${step.step_id}`}
                    >
                      <div className="ac-step-header">
                        <div>
                          <span className="ac-step-order">#{step.step_order}</span>
                          <strong>{step.title}</strong>
                        </div>
                        <div className="ac-step-badges">
                          <span className="ac-tool-badge">{step.tool_name}</span>
                          <span className={`ac-status-badge status-${step.status.toLowerCase()}`}>
                            {step.status}
                          </span>
                        </div>
                      </div>

                      <div className="ac-step-details">
                        <div>
                          <span className="ac-detail-label">Parameters:</span>{" "}
                          <code>{JSON.stringify(step.parameters)}</code>
                        </div>
                        <div>
                          <span className="ac-detail-label">Dependencies:</span>{" "}
                          <code>
                            {step.depends_on.length > 0 ? step.depends_on.join(", ") : "None"}
                          </code>
                          {" | "}
                          <span className="ac-detail-label">Attempts:</span>{" "}
                          <strong>
                            {step.attempt_count}/{step.max_retries + 1}
                          </strong>
                        </div>
                        <div>
                          <span className="ac-detail-label">Success Condition:</span>{" "}
                          <span>{step.expected_condition}</span>
                        </div>
                      </div>

                      {step.verification && step.verification.summary && (
                        <div
                          className={`ac-verification-box ${
                            step.verification.verified ? "verified-ok" : "verified-fail"
                          }`}
                        >
                          <strong>
                            {step.verification.verified ? "✓ VERIFIED:" : "✕ UNVERIFIED:"}
                          </strong>{" "}
                          {step.verification.summary}
                        </div>
                      )}

                      {step.error && step.error.message && (
                        <div className="ac-step-error-box">
                          <strong>Error ({step.error.error_code}):</strong> {step.error.message}
                        </div>
                      )}
                    </div>
                  ))
                )}

                {activeTask?.final_report && (
                  <div className="ac-final-report" data-testid="final-report">
                    <h4>Final Execution Report</h4>
                    <pre>{activeTask.final_report}</pre>
                  </div>
                )}
              </div>
            )}

            {/* TAB 2: Verification Evidence */}
            {activeTab === "verification" && (
              <div className="ac-verification-tab" data-testid="verification-tab">
                {displayedSteps.length === 0 ? (
                  <p className="ac-muted">No steps available to inspect verification evidence.</p>
                ) : (
                  displayedSteps.map((step) => (
                    <div key={step.step_id} className="ac-evidence-card">
                      <h4>
                        {step.step_id}: {step.title} ({step.tool_name})
                      </h4>
                      <p>
                        <strong>Verified:</strong>{" "}
                        {step.verification?.verified === true
                          ? "YES (Backed by Execution Evidence)"
                          : "NO"}
                      </p>
                      <pre className="ac-json-block">
                        {JSON.stringify(
                          {
                            execution_output: step.execution_output,
                            verification: step.verification,
                          },
                          null,
                          2
                        )}
                      </pre>
                    </div>
                  ))
                )}
              </div>
            )}

            {/* TAB 3: Errors & Recovery Attempts */}
            {activeTab === "recovery" && (
              <div className="ac-recovery-tab" data-testid="recovery-tab">
                {recoveryAndErrorLogs.length === 0 ? (
                  <p className="ac-muted">
                    Zero errors or recovery events recorded for the current task.
                  </p>
                ) : (
                  recoveryAndErrorLogs.map((log) => (
                    <div key={log.log_id} className={`ac-log-row level-${log.level.toLowerCase()}`}>
                      <div className="ac-log-meta">
                        <span className="ac-badge">{log.level}</span>
                        <span className="ac-badge neutral">{log.phase}</span>
                        {log.step_id && <code>{log.step_id}</code>}
                        <span className="ac-muted">{log.timestamp}</span>
                      </div>
                      <p>{log.message}</p>
                      {log.evidence && Object.keys(log.evidence).length > 0 && (
                        <pre className="ac-json-block">
                          {JSON.stringify(log.evidence, null, 2)}
                        </pre>
                      )}
                    </div>
                  ))
                )}
              </div>
            )}

            {/* TAB 4: Tool Registry */}
            {activeTab === "tools" && (
              <div className="ac-tools-grid" data-testid="tools-tab">
                {tools.map((tool) => (
                  <div key={tool.name} className="ac-tool-card">
                    <div className="ac-tool-card-top">
                      <strong>{tool.name}</strong>
                      <span
                        className={`ac-pill ${
                          tool.supported_on_current_platform ? "ok" : "neutral"
                        }`}
                      >
                        {tool.category}
                      </span>
                    </div>
                    <p>{tool.description}</p>
                    <div className="ac-tool-meta">
                      <span>Permission: {tool.permission_level}</span>
                      <span>Verify: {tool.verification_method}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}

export default App;
