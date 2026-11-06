/**
 * TypeScript Interfaces for AUTOCRAFT AI Frontend Dashboard.
 * Strictly aligned with FastAPI backend schemas.
 */

export type ExecutionPhase =
  | "IDLE"
  | "OBSERVE"
  | "PLAN"
  | "VALIDATE"
  | "ACT"
  | "VERIFY"
  | "RECOVER"
  | "EMERGENCY_STOPPED";

export type TaskStatus =
  | "PENDING"
  | "OBSERVING"
  | "PLANNING"
  | "VALIDATING"
  | "RUNNING"
  | "WAITING_APPROVAL"
  | "RECOVERING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export interface VerificationInfo {
  verified?: boolean;
  summary?: string;
  evidence?: Record<string, unknown>;
  failure_reason?: string | null;
}

export interface StepErrorInfo {
  error_code?: string;
  error_category?: string;
  message?: string;
}

export interface TaskStep {
  step_id: string;
  task_id?: string;
  step_order: number;
  title: string;
  tool_name: string;
  parameters: Record<string, unknown>;
  depends_on: string[];
  expected_condition: string;
  requires_approval: boolean;
  approval_reason?: string | null;
  status: string;
  attempt_count: number;
  max_retries: number;
  fallback_tool?: string | null;
  fallback_parameters?: Record<string, unknown> | null;
  execution_output: Record<string, unknown>;
  verification: VerificationInfo;
  error: StepErrorInfo;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface ApprovalRequest {
  approval_id: string;
  task_id: string;
  step_id: string;
  tool_name: string;
  action_summary: string;
  risk_level: string;
  reason: string;
  parameters: Record<string, unknown>;
  status: "PENDING" | "APPROVED" | "REJECTED";
  created_at: string;
  resolved_at?: string | null;
  resolution_note?: string | null;
}

export interface ExecutionLog {
  log_id: number;
  task_id: string;
  step_id?: string | null;
  phase: string;
  level: "INFO" | "WARN" | "ERROR" | "RECOVERY" | "SECURITY";
  message: string;
  evidence: Record<string, unknown>;
  timestamp: string;
}

export interface EnvironmentObservation {
  timestamp?: string;
  platform?: string;
  os_release?: string;
  workspace_root?: string;
  workspace_entries?: string[];
  desktop_automation_supported?: boolean;
  desktop_error?: string | null;
  open_windows_count?: number;
  browser_engine?: string;
  browser_available?: boolean;
  browser_error?: string | null;
  workspace_error?: string | null;
  browser_state?: Record<string, unknown>;
  emergency_stop_active?: boolean;
}

export interface TaskRecord {
  task_id: string;
  goal: string;
  detected_language: string;
  status: TaskStatus;
  current_phase: ExecutionPhase;
  current_step_id?: string | null;
  observation: EnvironmentObservation;
  plan: TaskStep[];
  steps: TaskStep[];
  approvals: ApprovalRequest[];
  logs: ExecutionLog[];
  final_report?: string | null;
  error_summary?: string | null;
  created_at: string;
  updated_at: string;
}

export interface HealthData {
  status: string;
  app_name: string;
  version: string;
  platform: string;
  database: {
    healthy: boolean;
    db_path: string;
    error?: string;
    total_tasks?: number;
    pending_approvals?: number;
  };
  workspace_root: string;
  browser_engine: string;
  browser_available: boolean;
  browser_error?: string | null;
  desktop_automation_supported: boolean;
  desktop_error?: string | null;
  discovered_applications: Record<
    string,
    { available: boolean; executable?: string | null; reason?: string; capabilities?: string[] }
  >;
  emergency_stop_active: boolean;
  registered_tool_count: number;
}

export interface ToolMetadata {
  name: string;
  category: string;
  description: string;
  capabilities: string[];
  permission_level: string;
  preconditions: string[];
  verification_method: string;
  supported_on_current_platform: boolean;
}

export interface PlanPreviewResponse {
  plan: {
    goal: string;
    detected_language: string;
    normalized_clauses: string[];
    steps: TaskStep[];
  };
  validation: {
    valid: boolean;
    errors: string[];
    warnings: string[];
    approval_required_steps: string[];
  };
}
