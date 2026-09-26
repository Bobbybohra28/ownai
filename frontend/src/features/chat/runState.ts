import type { RunEvent } from "@/api/sse";

export interface StepState {
  step: string;
  agent: string;
  agentName?: string;
  status: string;
  summary?: string;
  models: string[];
  tools: { tool: string; status: string; summary: string }[];
  error?: string | null;
  durationMs?: number;
}

export interface LiveRun {
  runId: string;
  status: string;
  messages: string[];
  intent?: { intent: string; mode: string; complexity: string };
  plan?: { agent: string; id: string }[];
  steps: Record<string, StepState>;
  order: string[];
  models: string[];
  notices: { level: string; message: string }[];
  verification: { source: string; status?: string; verdict?: string; summary: string }[];
  streamed: string;
  approvals: { approval_id: string; title: string; risk_level: string }[];
  error?: { code: string; message: string; hint?: string };
  done: boolean;
}

export function initialRun(runId: string): LiveRun {
  return { runId, status: "queued", messages: [], steps: {}, order: [], models: [], notices: [], verification: [], streamed: "", approvals: [], done: false };
}

export function reduceRun(state: LiveRun, event: RunEvent): LiveRun {
  const d = event.data ?? {};
  const s = { ...state };
  switch (event.type) {
    case "run_started":
      s.status = "running";
      break;
    case "status":
      s.messages = [...s.messages, d.message].slice(-12);
      break;
    case "intent":
      s.intent = { intent: d.intent, mode: d.mode, complexity: d.complexity };
      break;
    case "plan":
      s.plan = d.steps;
      break;
    case "step_started": {
      s.steps = { ...s.steps, [d.step]: { step: d.step, agent: d.agent, agentName: d.agent_name, status: "running", models: [], tools: [] } };
      if (!s.order.includes(d.step)) s.order = [...s.order, d.step];
      break;
    }
    case "step_finished": {
      const prev = s.steps[d.step] ?? { step: d.step, agent: d.agent, status: "running", models: [], tools: [] };
      s.steps = { ...s.steps, [d.step]: { ...prev, status: d.status, summary: d.summary, error: d.error, durationMs: d.duration_ms } };
      break;
    }
    case "model_selected": {
      if (!s.models.includes(d.model_id)) s.models = [...s.models, d.model_id];
      const key = Object.keys(s.steps).reverse().find((k) => s.steps[k].agent === d.agent && s.steps[k].status === "running");
      if (key && !s.steps[key].models.includes(d.model_id))
        s.steps = { ...s.steps, [key]: { ...s.steps[key], models: [...s.steps[key].models, d.model_id] } };
      break;
    }
    case "tool_finished": {
      const key = Object.keys(s.steps).reverse().find((k) => s.steps[k].agent === d.agent && s.steps[k].status === "running");
      if (key) s.steps = { ...s.steps, [key]: { ...s.steps[key], tools: [...s.steps[key].tools, { tool: d.tool, status: d.status, summary: d.summary }] } };
      break;
    }
    case "notice":
      s.notices = [...s.notices, { level: d.level, message: d.message }];
      break;
    case "verification":
      s.verification = [...s.verification, { source: d.source, status: d.status, verdict: d.verdict, summary: d.summary }];
      break;
    case "token":
      s.streamed += d.text ?? "";
      break;
    case "approval_required":
      s.approvals = [...s.approvals, { approval_id: d.approval_id, title: d.title, risk_level: d.risk_level }];
      break;
    case "error":
      s.error = { code: d.code, message: d.message, hint: d.hint };
      break;
    case "run_paused":
      s.status = "awaiting_approval";
      s.done = true;
      break;
    case "run_finished":
      s.status = d.status ?? "finished";
      s.done = true;
      break;
  }
  return s;
}
