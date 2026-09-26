import { useState } from "react";
import { api } from "@/api/client";
import type { ModelInfo, Project } from "@/api/types";
import { ErrorAlert, PageHeader, Spinner, StatusBadge, formatDate, useAction, useAsync, usePolling } from "@/components/ui";

export default function Evaluation() {
  const datasets = useAsync(() => api<any[]>("/evaluation/datasets"));
  const models = useAsync(() => api<{ models: ModelInfo[] }>("/models"));
  const projects = useAsync(() => api<Project[]>("/projects"));
  const runs = useAsync(() => api<any[]>("/evaluation/runs"));
  const [dataset, setDataset] = useState("");
  const [modelId, setModelId] = useState("");
  const [projectId, setProjectId] = useState("");
  const [detail, setDetail] = useState<any>(null);
  const active = runs.data?.some((r) => ["queued", "running"].includes(r.status)) ?? false;
  usePolling(() => void runs.reload(), 4000, active);

  const selected = datasets.data?.find((d) => d.name === dataset);
  const start = useAction(async () => {
    await api("/evaluation/runs", { body: { dataset, model_id: modelId, project_id: selected?.category === "rag" ? projectId : null } });
    await runs.reload();
  });
  const open = useAction(async (id: string) => setDetail(await api(`/evaluation/runs/${id}`)));
  const apply = useAction(async (id: string) => {
    const r = await api(`/evaluation/runs/${id}/apply-priority`, { method: "POST" });
    alert(`Routing priority of ${r.model_id} set to ${r.priority} (from measured mean score).`);
  });
  const chatModels = models.data?.models.filter((m) => m.capabilities.supports_chat) ?? [];

  return (
    <>
      <PageHeader title="Evaluation" />
      <p className="muted small">Benchmarks run real tasks against a model: generated code is executed against tests in the sandbox, SQL is executed on a real database, tool choices and retrieval are checked. Use results — not assumptions — to set routing priorities.</p>
      <div className="card">
        <div className="row">
          <select value={dataset} onChange={(e) => setDataset(e.target.value)} style={{ width: 260 }}>
            <option value="">Choose dataset…</option>
            {datasets.data?.map((d) => <option key={d.name} value={d.name}>{d.name} ({d.category}, {d.cases} cases)</option>)}
          </select>
          <select value={modelId} onChange={(e) => setModelId(e.target.value)} style={{ width: 200 }}>
            <option value="">Choose model…</option>
            {chatModels.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
          </select>
          {selected?.category === "rag" && (
            <select value={projectId} onChange={(e) => setProjectId(e.target.value)} style={{ width: 200 }}>
              <option value="">Indexed project…</option>
              {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          )}
          <button className="btn btn-primary" disabled={!dataset || !modelId || start.busy} onClick={() => void start.run()}>Run evaluation</button>
        </div>
        {selected && <p className="small muted">{selected.description}</p>}
        <ErrorAlert error={start.error || runs.error || apply.error} />
      </div>
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Runs</h2>
        {runs.loading && !runs.data ? <Spinner /> : (
          <table>
            <thead><tr><th>Dataset</th><th>Model</th><th>Status</th><th>Accuracy</th><th>Mean score</th><th>Latency p50</th><th>Tok/s</th><th>Tool errors</th><th>Verification failures</th><th /></tr></thead>
            <tbody>
              {runs.data?.map((r) => (
                <tr key={r.id}>
                  <td>{r.dataset}<div className="small muted">{formatDate(r.created_at)}</div></td>
                  <td>{r.model_id}</td>
                  <td><StatusBadge status={r.status} />{r.error && <div className="small">{r.error}</div>}</td>
                  <td>{r.metrics?.accuracy != null ? `${Math.round(r.metrics.accuracy * 100)}% (${r.passed}/${r.total})` : "—"}</td>
                  <td>{r.metrics?.mean_score ?? "—"}</td>
                  <td>{r.metrics?.latency_ms_p50 != null ? `${r.metrics.latency_ms_p50} ms` : "—"}</td>
                  <td>{r.metrics?.tokens_per_second ?? "—"}</td>
                  <td>{r.metrics?.tool_errors ?? "—"}</td>
                  <td>{r.metrics?.verification_failures ?? "—"}</td>
                  <td className="row">
                    <button className="btn btn-sm" onClick={() => void open.run(r.id)}>Details</button>
                    {r.status === "completed" && <button className="btn btn-sm" onClick={() => void apply.run(r.id)}>Use for routing</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {detail && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>{detail.dataset} · {detail.model_id}</h2>
          <table>
            <thead><tr><th>Case</th><th>Result</th><th>Score</th><th>Latency</th><th>Error</th><th>Detail</th></tr></thead>
            <tbody>
              {detail.results.map((r: any) => (
                <tr key={r.case_id}>
                  <td>{r.case_id}</td><td><StatusBadge status={r.passed ? "passed" : "failed"} /></td><td>{r.score}</td><td>{r.latency_ms} ms</td>
                  <td className="small">{r.error_code}</td><td className="mono small" style={{ maxWidth: 480, whiteSpace: "pre-wrap" }}>{JSON.stringify(r.detail).slice(0, 400)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
