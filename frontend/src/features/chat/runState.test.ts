import { initialRun, reduceRun } from "./runState";

describe("reduceRun", () => {
  it("tracks steps, models, tools, streamed tokens and terminal state", () => {
    let s = initialRun("r1");
    const events: [string, any][] = [
      ["run_started", {}],
      ["intent", { intent: "fix_bug", mode: "developer", complexity: "simple" }],
      ["step_started", { step: "diagnose", agent: "debugging", agent_name: "Debugging Agent" }],
      ["model_selected", { agent: "debugging", model_id: "coding" }],
      ["tool_finished", { agent: "debugging", tool: "run_tests", status: "error", summary: "3 failed" }],
      ["step_finished", { step: "diagnose", status: "succeeded", summary: "root cause found", duration_ms: 1200 }],
      ["token", { text: "Hello " }],
      ["token", { text: "world" }],
      ["verification", { source: "critic", verdict: "pass", summary: "ok" }],
      ["approval_required", { approval_id: "a1", title: "Apply", risk_level: "high" }],
      ["run_paused", { status: "awaiting_approval" }],
    ];
    events.forEach(([type, data], i) => (s = reduceRun(s, { id: i + 1, type, data })));
    expect(s.order).toEqual(["diagnose"]);
    expect(s.steps.diagnose.models).toEqual(["coding"]);
    expect(s.steps.diagnose.tools[0].tool).toBe("run_tests");
    expect(s.steps.diagnose.status).toBe("succeeded");
    expect(s.streamed).toBe("Hello world");
    expect(s.approvals).toHaveLength(1);
    expect(s.done).toBe(true);
    expect(s.status).toBe("awaiting_approval");
  });

  it("records errors", () => {
    const s = reduceRun(initialRun("r"), { id: 1, type: "error", data: { code: "MODEL_EMPTY_RESPONSE", message: "empty" } });
    expect(s.error?.code).toBe("MODEL_EMPTY_RESPONSE");
  });
});
