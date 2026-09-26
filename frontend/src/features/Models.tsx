import { useState } from "react";
import { api } from "@/api/client";
import type { HealthReport, ModelInfo } from "@/api/types";
import { ErrorAlert, PageHeader, Spinner, StatusBadge, formatDate, useAction, useAsync } from "@/components/ui";

function HealthSteps({ report }: { report: HealthReport }) {
  return (
    <table>
      <tbody>
        {report.steps.map((s, i) => (
          <tr key={i}>
            <td><StatusBadge status={s.ok ? "passed" : "failed"} /></td>
            <td className="mono small">{s.name}</td>
            <td className="small">{s.message}{s.code && <span className="muted"> ({s.code})</span>}</td>
            <td className="small muted">{s.latency_ms != null ? `${s.latency_ms} ms` : ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function Models() {
  const models = useAsync(() => api<{ models: ModelInfo[]; warnings: { model_id: string; message: string }[] }>("/models"));
  const usage = useAsync(() => api<any[]>("/models/usage"));
  const [tests, setTests] = useState<Record<string, HealthReport>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [preview, setPreview] = useState<any>(null);
  const [role, setRole] = useState("coding");
  const [complexity, setComplexity] = useState("simple");

  const test = useAction(async (id: string) => {
    setTesting(id);
    try {
      const report = await api<HealthReport>(`/models/${id}/test`, { method: "POST" });
      setTests((t) => ({ ...t, [id]: report }));
      await models.reload();
    } finally {
      setTesting(null);
    }
  });
  const refreshAll = useAction(async () => {
    await api("/models/health", { query: { refresh: true } });
    await models.reload();
  });
  const route = useAction(async () => setPreview(await api("/models/routing/preview", { query: { role, complexity } })));
  const toggle = useAction(async (m: ModelInfo) => {
    await api(`/models/${m.id}`, { method: "PATCH", body: { enabled: !m.enabled } });
    await models.reload();
  });

  return (
    <>
      <PageHeader title="Models" actions={<button className="btn" disabled={refreshAll.busy} onClick={() => void refreshAll.run()}>{refreshAll.busy ? "Checking…" : "Re-check all"}</button>} />
      <p className="muted small">A model is shown <strong>ONLINE</strong> only after a real check: endpoint reachable, model name served, and a real completion (or embedding) returned non-empty output.</p>
      <ErrorAlert error={models.error || test.error || refreshAll.error || toggle.error} />
      {models.data?.warnings.map((w) => <div key={w.model_id} className="alert alert-warn small"><strong>{w.model_id}</strong>: {w.message}</div>)}
      {models.loading && !models.data ? <Spinner /> : (
        <div className="card">
          <table>
            <thead>
              <tr><th>Model</th><th>Status</th><th>Endpoint</th><th>Roles</th><th>Latency</th><th>Context</th><th>Capabilities</th><th /></tr>
            </thead>
            <tbody>
              {models.data?.models.map((m) => (
                <tr key={m.id}>
                  <td><strong>{m.id}</strong><div className="mono small muted">{m.model}</div><div className="small muted">{m.provider} · priority {m.priority}</div></td>
                  <td><StatusBadge status={m.online ? "online" : m.status} />
                    {m.health?.error && <div className="small" style={{ color: "var(--danger)", maxWidth: 240 }}>{m.health.error_code}: {m.health.error}</div>}
                    <div className="small muted">{m.health ? formatDate(m.health.checked_at) : "never checked"}</div>
                  </td>
                  <td className="mono small">{m.endpoint}</td>
                  <td>{m.roles.map((r) => <span key={r} className="badge">{r}</span>)}</td>
                  <td className="small">{m.health?.latency_ms != null ? `${m.health.latency_ms} ms` : "—"}</td>
                  <td className="small">{m.context_length.toLocaleString()}</td>
                  <td className="small">{Object.entries(m.capabilities).filter(([, v]) => v).map(([k]) => k.replace("supports_", "")).join(", ")}</td>
                  <td>
                    <button className="btn btn-sm" disabled={testing === m.id} onClick={() => void test.run(m.id)}>{testing === m.id ? "Testing…" : "Test connection"}</button>
                    <button className="btn btn-sm" style={{ marginLeft: 4 }} onClick={() => void toggle.run(m)}>{m.enabled ? "Disable" : "Enable"}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {Object.entries(tests).map(([id, report]) => (
        <div key={id} className="card">
          <div className="row"><strong>Connection test: {id}</strong> <StatusBadge status={report.status} /></div>
          <HealthSteps report={report} />
        </div>
      ))}
      <div className="grid grid-2" style={{ marginTop: 12 }}>
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Routing preview</h2>
          <div className="row">
            <select value={role} onChange={(e) => setRole(e.target.value)} style={{ width: 160 }}>
              {["fast", "coding", "reasoning", "vision", "embedding", "reranker"].map((r) => <option key={r}>{r}</option>)}
            </select>
            <select value={complexity} onChange={(e) => setComplexity(e.target.value)} style={{ width: 160 }}>
              {["trivial", "simple", "moderate", "complex"].map((r) => <option key={r}>{r}</option>)}
            </select>
            <button className="btn" onClick={() => void route.run()}>Explain routing</button>
          </div>
          <ErrorAlert error={route.error} />
          {preview && (
            <>
              <p>Selected: <strong>{preview.decision?.model_id ?? "none"}</strong> {preview.decision?.error && <span style={{ color: "var(--danger)" }}>{preview.decision.error}</span>}</p>
              <table><tbody>{preview.candidates.map((c: any) => (
                <tr key={c.model_id}><td>{c.model_id}</td><td>{c.eligible ? <StatusBadge status="ok" /> : <span className="badge badge-danger">{c.rejected_reason}</span>}</td><td className="small">{c.score}</td><td className="small muted">{c.reasons.join("; ")}</td></tr>
              ))}</tbody></table>
            </>
          )}
        </div>
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Usage (7 days)</h2>
          <table>
            <thead><tr><th>Model</th><th>Op</th><th>Calls</th><th>Success</th><th>Avg latency</th><th>Tokens in/out</th></tr></thead>
            <tbody>{usage.data?.map((u, i) => (
              <tr key={i}><td>{u.model_id}</td><td>{u.operation}</td><td>{u.calls}</td><td>{u.success_rate != null ? `${Math.round(u.success_rate * 100)}%` : "—"}</td><td>{u.avg_latency_ms} ms</td><td className="small">{u.prompt_tokens}/{u.completion_tokens}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </div>
    </>
  );
}
