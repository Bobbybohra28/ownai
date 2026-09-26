import { api } from "@/api/client";
import type { AgentSpec } from "@/api/types";
import { ErrorAlert, PageHeader, Spinner, StatusBadge, formatDate, useAction, useAsync } from "@/components/ui";

export default function Agents() {
  const agents = useAsync(() => api<AgentSpec[]>("/agents"));
  const activity = useAsync(() => api<any[]>("/agents/activity", { query: { limit: 60 } }));
  const toggle = useAction(async (a: AgentSpec) => {
    await api(`/agents/${a.id}`, { method: "PATCH", body: { enabled: !a.enabled } });
    await agents.reload();
  });
  return (
    <>
      <PageHeader title="Agents" actions={<button className="btn" onClick={() => void activity.reload()}>Refresh monitor</button>} />
      <p className="muted small">Agents are defined in <span className="mono">config/agents/*.yaml</span>. The planner/orchestrator choose which agents a request needs — not every agent runs every time.</p>
      <ErrorAlert error={agents.error || toggle.error} />
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Agent monitor</h2>
        {activity.loading && !activity.data ? <Spinner /> : activity.data?.length ? (
          <table>
            <thead><tr><th>Agent</th><th>Status</th><th>Model</th><th>Task</th><th>Duration</th><th>Tools</th><th>Result / error</th><th>Started</th></tr></thead>
            <tbody>
              {activity.data.map((a, i) => (
                <tr key={i}>
                  <td>{a.agent}</td><td><StatusBadge status={a.status} /></td><td className="small">{a.model ?? "—"}</td>
                  <td className="small">{a.task}</td><td className="small">{a.duration_ms != null ? `${(a.duration_ms / 1000).toFixed(1)}s` : "—"}</td>
                  <td className="small mono">{a.tools.join(", ")}</td>
                  <td className="small">{a.error ? <span style={{ color: "var(--danger)" }}>{a.error}</span> : a.result}</td>
                  <td className="small muted">{formatDate(a.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted">No agent activity yet.</p>}
      </div>
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Catalogue</h2>
        <table>
          <thead><tr><th>Agent</th><th>Category</th><th>Runtime</th><th>Model role</th><th>Tools</th><th>Verification</th><th /></tr></thead>
          <tbody>
            {agents.data?.map((a) => (
              <tr key={a.id}>
                <td><strong>{a.name}</strong><div className="small muted">{a.description}</div></td>
                <td>{a.category}</td>
                <td className="small">{a.runtime}{a.output_schema && <div className="muted">→ {a.output_schema}</div>}</td>
                <td className="small">{a.model_role}{a.complex_model_role && a.complex_model_role !== a.model_role ? ` / ${a.complex_model_role} (complex)` : ""}</td>
                <td className="small mono">{a.allowed_tools.join(", ") || "—"}</td>
                <td className="small">{Object.entries(a.verification).filter(([, v]) => v).map(([k]) => k).join(", ") || "—"}</td>
                <td><button className="btn btn-sm" onClick={() => void toggle.run(a)}>{a.enabled ? "Disable" : "Enable"}</button> {!a.enabled && <StatusBadge status="disabled" />}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
