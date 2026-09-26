import { useState } from "react";
import { api } from "@/api/client";
import type { Project } from "@/api/types";
import { ErrorAlert, PageHeader, Spinner, formatDate, useAction, useAsync } from "@/components/ui";

const KINDS = ["preference", "style", "decision", "fact", "architecture", "note"];

export default function MemoryPage() {
  const memories = useAsync(() => api<any[]>("/memory"));
  const projects = useAsync(() => api<Project[]>("/projects"));
  const [scope, setScope] = useState<"user" | "project">("user");
  const [kind, setKind] = useState("preference");
  const [content, setContent] = useState("");
  const [projectId, setProjectId] = useState("");
  const add = useAction(async () => {
    await api("/memory", { body: { scope, kind, content, project_id: scope === "project" ? projectId : null } });
    setContent("");
    await memories.reload();
  });
  const remove = useAction(async (id: string) => {
    await api(`/memory/${id}`, { method: "DELETE", allowEmpty: true });
    await memories.reload();
  });
  return (
    <>
      <PageHeader title="Memory" />
      <p className="muted small">Preferences, coding style and project decisions the agents should remember. Secrets are rejected automatically and never stored.</p>
      <div className="card">
        <div className="row">
          <select value={scope} onChange={(e) => setScope(e.target.value as "user" | "project")} style={{ width: 140 }}>
            <option value="user">Me (user)</option>
            <option value="project">Project</option>
          </select>
          {scope === "project" && (
            <select value={projectId} onChange={(e) => setProjectId(e.target.value)} style={{ width: 200 }}>
              <option value="">Choose project…</option>
              {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          )}
          <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 150 }}>
            {KINDS.map((k) => <option key={k}>{k}</option>)}
          </select>
          <input value={content} onChange={(e) => setContent(e.target.value)} placeholder='e.g. "Prefer pytest fixtures over unittest classes"' style={{ flex: 1 }} />
          <button className="btn btn-primary" disabled={add.busy || !content || (scope === "project" && !projectId)} onClick={() => void add.run()}>Remember</button>
        </div>
        <ErrorAlert error={add.error || remove.error || memories.error} />
      </div>
      <div className="card">
        {memories.loading ? <Spinner /> : (
          <table>
            <thead><tr><th>Scope</th><th>Kind</th><th>Content</th><th>Source</th><th>Created</th><th /></tr></thead>
            <tbody>
              {memories.data?.map((m) => (
                <tr key={m.id}>
                  <td>{m.scope}</td><td>{m.kind}</td><td>{m.content}</td><td className="small">{m.source}</td>
                  <td className="small muted">{formatDate(m.created_at)}</td>
                  <td><button className="btn btn-sm btn-danger" onClick={() => void remove.run(m.id)}>Forget</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
