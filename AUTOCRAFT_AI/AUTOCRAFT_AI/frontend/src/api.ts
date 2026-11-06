/**
 * REST API Client for AUTOCRAFT AI Backend.
 * Ensures consistent route paths and accurate error propagation.
 */

import {
  ApprovalRequest,
  EnvironmentObservation,
  HealthData,
  PlanPreviewResponse,
  TaskRecord,
  ToolMetadata,
} from "./types";

const API_BASE =
  typeof window !== "undefined" && (window as unknown as { __AUTOCRAFT_API_BASE__?: string }).__AUTOCRAFT_API_BASE__
    ? (window as unknown as { __AUTOCRAFT_API_BASE__: string }).__AUTOCRAFT_API_BASE__
    : "";

async function requestJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
    ...options,
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status} while requesting ${path}`;
    try {
      const errBody = await response.json();
      detail = errBody.detail || errBody.message || detail;
    } catch {
      // fallback to status text
    }
    throw new Error(detail);
  }

  return response.json() as Promise<T>;
}

export const AutocraftApi = {
  getHealth(): Promise<HealthData> {
    return requestJson<HealthData>("/api/health");
  },

  getObservation(): Promise<EnvironmentObservation> {
    return requestJson<EnvironmentObservation>("/api/observe");
  },

  listTools(): Promise<{ count: number; tools: ToolMetadata[] }> {
    return requestJson<{ count: number; tools: ToolMetadata[] }>("/api/tools");
  },

  previewPlan(goal: string): Promise<PlanPreviewResponse> {
    return requestJson<PlanPreviewResponse>("/api/plan/preview", {
      method: "POST",
      body: JSON.stringify({ goal }),
    });
  },

  startTask(goal: string, runAsync: boolean = false): Promise<TaskRecord> {
    return requestJson<TaskRecord>("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ goal, run_async: runAsync }),
    });
  },

  listTasks(limit: number = 50): Promise<{ count: number; tasks: TaskRecord[] }> {
    return requestJson<{ count: number; tasks: TaskRecord[] }>(`/api/tasks?limit=${limit}`);
  },

  getTask(taskId: string): Promise<TaskRecord> {
    return requestJson<TaskRecord>(`/api/tasks/${encodeURIComponent(taskId)}`);
  },

  cancelTask(taskId: string, reason: string = "Stopped by user from dashboard."): Promise<TaskRecord> {
    return requestJson<TaskRecord>(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
  },

  resumeTask(taskId: string): Promise<TaskRecord> {
    return requestJson<TaskRecord>(`/api/tasks/${encodeURIComponent(taskId)}/resume`, {
      method: "POST",
    });
  },

  clearHistory(): Promise<{ deleted_tasks: number }> {
    return requestJson<{ deleted_tasks: number }>("/api/tasks/history", {
      method: "DELETE",
    });
  },

  listPendingApprovals(taskId?: string): Promise<{ count: number; approvals: ApprovalRequest[] }> {
    const query = taskId ? `?task_id=${encodeURIComponent(taskId)}` : "";
    return requestJson<{ count: number; approvals: ApprovalRequest[] }>(`/api/approvals${query}`);
  },

  resolveApproval(
    approvalId: string,
    approved: boolean,
    resolutionNote: string = ""
  ): Promise<{ approval: ApprovalRequest; task: TaskRecord }> {
    return requestJson<{ approval: ApprovalRequest; task: TaskRecord }>(
      `/api/approvals/${encodeURIComponent(approvalId)}/resolve`,
      {
        method: "POST",
        body: JSON.stringify({ approved, resolution_note: resolutionNote }),
      }
    );
  },

  emergencyStop(): Promise<{ emergency_stop_active: boolean; halted_task_ids: string[] }> {
    return requestJson<{ emergency_stop_active: boolean; halted_task_ids: string[] }>(
      "/api/emergency-stop",
      { method: "POST" }
    );
  },

  resetEmergencyStop(): Promise<{ emergency_stop_active: boolean }> {
    return requestJson<{ emergency_stop_active: boolean }>("/api/emergency-stop/reset", {
      method: "POST",
    });
  },
};
