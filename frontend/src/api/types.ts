export interface User {
  id: string;
  email: string;
  display_name: string;
  is_superadmin: boolean;
  preferences: Record<string, unknown>;
}

export interface Organization {
  id: string;
  name: string;
  slug: string;
  plan_id: string;
  is_personal: boolean;
}

export interface Session {
  user: User;
  organization: Organization;
  role: string;
}

export interface Project {
  id: string;
  name: string;
  slug: string;
  description: string;
  source_type: string;
  source_ref: string;
  linked: boolean;
  status: string;
  status_message: string;
  created_at: string;
  indexed_at: string | null;
  overview?: ProjectOverview;
  index_job?: IndexJob | null;
}

export interface IndexJob {
  id: string;
  status: string;
  stats: Record<string, number>;
  warnings: string[];
  error: string | null;
}

export interface ProjectOverview {
  file_count?: number;
  languages?: Record<string, number>;
  frameworks?: string[];
  dependencies?: { name: string; version: string | null; ecosystem: string; manifest: string; pinned: boolean; dev: boolean }[];
  endpoints?: { method: string; path: string; file: string; line: number; framework: string; handler?: string }[];
  env_vars?: string[];
  env_files?: Record<string, string[]>;
  database_usage?: { kind: string; file: string; line: number; snippet: string }[];
  tests?: { framework: string | null; command: string[] | null; test_files: string[] };
  sensitive_files?: string[];
  files_with_secrets?: string[];
  readme_excerpt?: string;
  tree?: string;
  git?: { is_repo: boolean; branch?: string };
}

export interface Conversation {
  id: string;
  title: string;
  project_id: string | null;
  mode: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant" | "system_note";
  content: string;
  run_id: string | null;
  created_at: string;
  meta: Record<string, any>;
}

export interface Approval {
  id: string;
  kind: string;
  tool: string | null;
  title: string;
  description: string;
  risk_level: string;
  status: string;
  args: Record<string, unknown>;
  run_id: string | null;
  project_id: string | null;
  created_at: string;
  changeset?: { id: string; status: string; stats: ChangeStats; title: string };
  result?: Record<string, unknown>;
}

export interface ChangeStats {
  files?: number;
  added?: number;
  removed?: number;
  paths?: { path: string; operation: string; added: number; removed: number }[];
}

export interface HealthStep {
  name: string;
  ok: boolean;
  code?: string | null;
  message: string;
  latency_ms?: number | null;
}

export interface HealthReport {
  model_id: string;
  status: string;
  checked_at: string;
  latency_ms?: number | null;
  error_code?: string | null;
  error?: string | null;
  steps: HealthStep[];
  available_models?: string[] | null;
}

export interface ModelInfo {
  id: string;
  provider: string;
  endpoint: string;
  model: string;
  roles: string[];
  context_length: number;
  max_output_tokens: number;
  capabilities: Record<string, boolean>;
  priority: number;
  enabled: boolean;
  status: string;
  online: boolean;
  health: HealthReport | null;
}

export interface AgentSpec {
  id: string;
  name: string;
  description: string;
  category: string;
  capabilities: string[];
  runtime: string;
  output_schema: string | null;
  allowed_tools: string[];
  model_role: string;
  complex_model_role: string | null;
  enabled: boolean;
  verification: Record<string, boolean>;
}
