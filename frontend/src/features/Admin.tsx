import { useState } from "react";
import { api } from "@/api/client";
import { ErrorAlert, PageHeader, Spinner, StatusBadge, formatDate, useAction, useAsync } from "@/components/ui";

export default function Admin() {
  const health = useAsync(() => api("/admin/health"));
  const audit = useAsync(() => api<any[]>("/admin/audit-logs", { query: { limit: 100 } }));
  const usage = useAsync(() => api("/admin/usage"));
  const [pipeline, setPipeline] = useState<any>(null);
  const [full, setFull] = useState(false);
  const diagnose = useAction(async () => {
    const result = await api("/admin/diagnostics/pipeline", { method: "POST", query: { full } });
    if (!result || !Array.isArray(result.steps)) throw new Error("FRONTEND_RESPONSE_ERROR: diagnostics response has no steps.");
    setPipeline(result);
  });

  return (
    <>
      <PageHeader title="Admin & diagnostics" />
      <ErrorAlert error={health.error} />
      <div className="card">
        <h2 style={{ marginTop: 0 }}>AI pipeline diagnostics</h2>
        <p className="muted small">Checks every boundary with real calls: services → each model (endpoint, /v1/models, model name, real completion content, streaming) → router → provider → response parser → agent → (optionally) the full orchestrator → API → this page. Use it when the server responds but AI answers are empty.</p>
        <div className="row">
          <label className="field-inline" style={{ margin: 0 }}><input type="checkbox" checked={full} onChange={(e) => setFull(e.target.checked)} /> include full orchestrator run</label>
          <button className="btn btn-primary" disabled={diagnose.busy} onClick={() => void diagnose.run()}>{diagnose.busy ? "Running…" : "Run diagnostics"}</button>
          {diagnose.busy && <Spinner label="This makes real model calls and can take a while on CPU." />}
        </div>
        <ErrorAlert error={diagnose.error} />
        {pipeline && (
          <>
            <p>{pipeline.ok ? <StatusBadge status="passed" /> : <><StatusBadge status="failed" /> First failing boundary: <strong>{pipeline.first_failure}</strong></>} <span className="muted small">frontend received and validated the response ✓</span></p>
            <table>
              <thead><tr><th>Boundary</th><th>Result</th><th>Latency</th><th>Details</th></tr></thead>
              <tbody>
                {pipeline.steps.map((s: any) => (
                  <tr key={s.step}>
                    <td className="mono">{s.step}</td>
                    <td><StatusBadge status={s.ok ? "passed" : "failed"} /></td>
                    <td className="small">{s.latency_ms} ms</td>
                    <td className="small">
                      {s.error_code && <strong>{s.error_code}: </strong>}{s.message}
                      {s.model_id && <div>model: {s.model_id}</div>}
                      {s.content_preview && <div>content: <span className="mono">{s.content_preview}</span></div>}
                      {s.checks && <div className="muted">{s.checks.map((c: any) => `${c.name}:${c.ok ? "ok" : "FAIL"}`).join(" · ")}</div>}
                      {s.hint && <div className="muted">{s.hint}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
      <div className="grid grid-2" style={{ marginTop: 12 }}>
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Services</h2>
          {health.loading ? <Spinner /> : (
            <table><tbody>{Object.entries(health.data ?? {}).map(([k, v]: [string, any]) => (
              <tr key={k}><td>{k}</td><td><StatusBadge status={v.ok ? "ok" : "failed"} /></td><td className="small mono">{JSON.stringify(v).slice(0, 200)}</td></tr>
            ))}</tbody></table>
          )}
        </div>
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Usage (30 days)</h2>
          <p className="small">Runs: {Object.entries(usage.data?.runs_by_status ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"}</p>
          <p className="small">Tokens: {usage.data?.tokens?.prompt ?? 0} in / {usage.data?.tokens?.completion ?? 0} out</p>
        </div>
      </div>
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Audit log</h2>
        <table>
          <thead><tr><th>Time</th><th>Action</th><th>Actor</th><th>Resource</th><th>Outcome</th><th>Request</th></tr></thead>
          <tbody>{audit.data?.map((a) => (
            <tr key={a.id}><td className="small muted">{formatDate(a.created_at)}</td><td className="mono small">{a.action}</td><td className="small">{a.actor_type}:{String(a.actor_id ?? "").slice(0, 12)}</td>
              <td className="small">{a.resource_type} {String(a.resource_id ?? "").slice(0, 12)}</td><td><StatusBadge status={a.outcome === "success" ? "ok" : a.outcome} /></td><td className="mono small muted">{String(a.request_id ?? "").slice(0, 8)}</td></tr>
          ))}</tbody>
        </table>
        <ErrorAlert error={audit.error} />
      </div>
    </>
  );
}
