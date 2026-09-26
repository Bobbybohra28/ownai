import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { Approval, Project, ProjectOverview } from "@/api/types";
import { DiffView, Empty, ErrorAlert, Spinner, StatusBadge, formatDate, useAction, useAsync } from "@/components/ui";

export function OverviewPanel({ project }: { project: Project }) {
  const ov: ProjectOverview = project.overview ?? {};
  return (
    <div className="grid grid-2">
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Summary</h3>
        <table>
          <tbody>
            <tr><th>Files indexed</th><td>{ov.file_count ?? "—"}</td></tr>
            <tr><th>Languages</th><td>{Object.entries(ov.languages ?? {}).map(([k, v]) => `${k} (${v})`).join(", ") || "—"}</td></tr>
            <tr><th>Frameworks</th><td>{ov.frameworks?.join(", ") || "—"}</td></tr>
            <tr><th>Tests</th><td>{ov.tests?.framework ? `${ov.tests.framework} — ${ov.tests.command?.join(" ")}` : "none detected"}</td></tr>
            <tr><th>Git</th><td>{ov.git?.is_repo ? `repository (${ov.git.branch ?? "?"})` : "not a git repository"}</td></tr>
            <tr><th>Credential files</th><td>{ov.sensitive_files?.join(", ") || "none"} <span className="muted small">(never indexed)</span></td></tr>
            <tr><th>Files with secrets</th><td>{ov.files_with_secrets?.join(", ") || "none"} <span className="muted small">(masked)</span></td></tr>
          </tbody>
        </table>
        {project.index_job && (
          <p className="small muted">
            Last index: <StatusBadge status={project.index_job.status} /> {Object.entries(project.index_job.stats ?? {}).map(([k, v]) => `${k}: ${v}`).join(" · ")}
          </p>
        )}
        {project.index_job?.warnings?.map((w) => <div key={w} className="alert alert-warn small">{w}</div>)}
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>API endpoints</h3>
        {ov.endpoints?.length ? (
          <table><tbody>{ov.endpoints.slice(0, 40).map((e, i) => (
            <tr key={i}><td className="mono">{e.method}</td><td className="mono">{e.path}</td><td className="small muted">{e.file}:{e.line}</td></tr>
          ))}</tbody></table>
        ) : <p className="muted">No HTTP endpoints detected.</p>}
        <h3>Environment variables (names only)</h3>
        <p className="mono small">{ov.env_vars?.join(", ") || "—"}</p>
        {Object.entries(ov.env_files ?? {}).map(([file, keys]) => (
          <p key={file} className="small"><span className="mono">{file}</span>: {keys.join(", ")} <span className="muted">(values hidden)</span></p>
        ))}
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Dependencies</h3>
        {ov.dependencies?.length ? (
          <table><tbody>{ov.dependencies.slice(0, 60).map((d, i) => (
            <tr key={i}><td className="mono">{d.name}</td><td className="mono small">{d.version ?? "any"}</td><td className="small muted">{d.ecosystem}{d.dev ? " (dev)" : ""}</td></tr>
          ))}</tbody></table>
        ) : <p className="muted">No dependency manifests found.</p>}
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Structure</h3>
        <div className="tree" style={{ maxHeight: 400, overflow: "auto" }}>{ov.tree || "—"}</div>
      </div>
    </div>
  );
}

export function FilesPanel({ project }: { project: Project }) {
  const tree = useAsync(() => api<{ files: { path: string; language: string; sensitive: boolean; has_secrets: boolean }[] }>(`/projects/${project.id}/files`), [project.id]);
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const content = useAsync(async () => (selected ? api(`/projects/${project.id}/files/content`, { query: { path: selected } }) : null), [selected]);
  const files = (tree.data?.files ?? []).filter((f) => f.path.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="grid" style={{ gridTemplateColumns: "300px 1fr" }}>
      <div className="card">
        <input placeholder="Filter files…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <ErrorAlert error={tree.error} />
        <div className="file-list" style={{ marginTop: 8 }}>
          {tree.loading ? <Spinner /> : files.map((f) => (
            <div key={f.path} className={selected === f.path ? "active" : ""} onClick={() => setSelected(f.path)} title={f.path}>
              {f.path} {f.sensitive && <span className="badge badge-warn">credential</span>} {f.has_secrets && <span className="badge badge-warn">secrets masked</span>}
            </div>
          ))}
        </div>
      </div>
      <div className="card">
        {!selected ? <Empty>Select a file to view it.</Empty> : content.loading ? <Spinner /> : content.error ? <ErrorAlert error={content.error} /> : (
          <>
            <div className="row"><strong className="mono">{selected}</strong></div>
            {content.data?.content != null ? (
              <div className="code-view" style={{ marginTop: 8 }}>
                {String(content.data.content).split("\n").map((line: string, i: number) => (
                  <div key={i}><span className="muted" style={{ display: "inline-block", width: 44, userSelect: "none" }}>{i + 1}</span>{line}</div>
                ))}
              </div>
            ) : content.data?.variables ? (
              <p>Credential file — variable names only: <span className="mono">{content.data.variables.join(", ")}</span></p>
            ) : <p className="muted">Binary file ({content.data?.size} bytes).</p>}
          </>
        )}
      </div>
    </div>
  );
}

export function ChangesPanel({ project }: { project: Project }) {
  const list = useAsync(() => api<any[]>(`/projects/${project.id}/changesets`), [project.id]);
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useAsync(async () => (selected ? api(`/projects/${project.id}/changesets/${selected}`) : null), [selected]);
  const approvals = useAsync(() => api<Approval[]>("/approvals"), [selected]);
  const pending = approvals.data?.find((a) => a.changeset?.id === selected && a.status === "pending");
  const decide = useAction(async (approve: boolean) => {
    if (!pending) return;
    await api(`/approvals/${pending.id}/${approve ? "approve" : "reject"}`, { body: {} });
    await Promise.all([list.reload(), detail.reload(), approvals.reload()]);
  });
  return (
    <div className="grid" style={{ gridTemplateColumns: "340px 1fr" }}>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Change sets</h3>
        <ErrorAlert error={list.error} />
        {list.data?.length ? list.data.map((c) => (
          <div key={c.id} className="file-list">
            <div className={selected === c.id ? "active" : ""} onClick={() => setSelected(c.id)}>
              <StatusBadge status={c.status} /> {c.title || "Proposed changes"}
              <div className="muted small">{c.stats?.files ?? 0} files · +{c.stats?.added ?? 0}/-{c.stats?.removed ?? 0} · {formatDate(c.created_at)}</div>
            </div>
          </div>
        )) : <p className="muted">No AI-proposed changes yet.</p>}
      </div>
      <div className="card">
        {!selected ? <Empty>Select a change set to review its diff.</Empty> : detail.loading ? <Spinner /> : detail.data && (
          <>
            <div className="row">
              <strong>{detail.data.title}</strong> <StatusBadge status={detail.data.status} />
              <span className="spacer" />
              {pending && (
                <>
                  <button className="btn btn-ok" disabled={decide.busy} onClick={() => void decide.run(true)}>Approve &amp; apply</button>
                  <button className="btn btn-danger" disabled={decide.busy} onClick={() => void decide.run(false)}>Reject</button>
                </>
              )}
            </div>
            {detail.data.run_id && <p className="small muted">Proposed by an agent run. Tests and verification results are shown in the chat report.</p>}
            <ErrorAlert error={decide.error} />
            {detail.data.files.map((f: any) => (
              <div key={f.path} style={{ marginTop: 12 }}>
                <div className="row"><span className="mono">{f.path}</span><span className="badge">{f.operation}</span><span className="small muted">+{f.added}/-{f.removed}</span></div>
                <DiffView diff={f.diff} />
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

export function GitPanel({ project }: { project: Project }) {
  const status = useAsync(() => api(`/projects/${project.id}/git/status`), [project.id]);
  const log = useAsync(() => api(`/projects/${project.id}/git/log`, { query: { limit: 20 } }), [project.id]);
  const diff = useAsync(() => api(`/projects/${project.id}/git/diff`), [project.id]);
  const [message, setMessage] = useState("");
  const commit = useAction(async () => {
    const result = await api(`/projects/${project.id}/git/commit`, { body: { message } });
    return result;
  });
  const [commitResult, setCommitResult] = useState<any>(null);
  if (status.data?.status === "error") return <div className="alert alert-info">{status.data.summary}</div>;
  return (
    <div className="grid grid-2">
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Status</h3>
        <ErrorAlert error={status.error} />
        {status.data && (
          <>
            <p>Branch <span className="mono">{status.data.data?.branch}</span> — {status.data.summary}</p>
            <table><tbody>{status.data.data?.files?.map((f: any) => <tr key={f.path}><td className="mono">{f.status}</td><td className="mono">{f.path}</td></tr>)}</tbody></table>
          </>
        )}
        <h3>Commit (requires approval)</h3>
        <input placeholder="Commit message" value={message} onChange={(e) => setMessage(e.target.value)} />
        <button className="btn" style={{ marginTop: 8 }} disabled={commit.busy || message.length < 3}
          onClick={async () => setCommitResult(await commit.run())}>Request commit</button>
        <ErrorAlert error={commit.error} />
        {commitResult && <div className="alert alert-info small">{commitResult.summary} {commitResult.approval_id && <Link to="/approvals">Open approvals →</Link>}</div>}
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Recent commits</h3>
        <table><tbody>{log.data?.data?.commits?.map((c: any) => (
          <tr key={c.hash}><td className="mono small">{c.hash}</td><td>{c.subject}</td><td className="small muted">{c.author}</td></tr>
        ))}</tbody></table>
      </div>
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <h3 style={{ marginTop: 0 }}>Uncommitted diff</h3>
        {diff.data?.data?.diff ? <DiffView diff={diff.data.data.diff} /> : <p className="muted">Working tree matches HEAD.</p>}
      </div>
    </div>
  );
}

export function SqlPanel({ project }: { project: Project }) {
  const connections = useAsync(() => api<any[]>(`/projects/${project.id}/db-connections`), [project.id]);
  const [selected, setSelected] = useState<string>("");
  const [query, setQuery] = useState("SELECT 1;");
  const [result, setResult] = useState<any>(null);
  const [validation, setValidation] = useState<any>(null);
  const schema = useAsync(async () => (selected ? api(`/db-connections/${selected}/schema`) : null), [selected]);
  const run = useAction(async () => setResult(await api(`/db-connections/${selected}/query`, { body: { query } })));
  const [form, setForm] = useState({ name: "", dialect: "sqlite", host: "", port: "", database: "", username: "", password: "", read_only: true });
  const create = useAction(async () => {
    await api(`/projects/${project.id}/db-connections`, { body: { ...form, port: form.port ? Number(form.port) : null, password: form.password || null } });
    await connections.reload();
  });
  useEffect(() => {
    if (!selected && connections.data?.length) setSelected(connections.data[0].id);
  }, [connections.data, selected]);
  useEffect(() => {
    const id = setTimeout(async () => setValidation(await api("/sql/validate", { body: { query, dialect: connections.data?.find((c) => c.id === selected)?.dialect ?? "postgresql" } }).catch(() => null)), 400);
    return () => clearTimeout(id);
  }, [query, selected, connections.data]);
  return (
    <div className="grid" style={{ gridTemplateColumns: "320px 1fr" }}>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Connections</h3>
        {connections.data?.map((c) => (
          <div key={c.id} className="file-list"><div className={selected === c.id ? "active" : ""} onClick={() => setSelected(c.id)}>
            {c.name} <span className="badge">{c.dialect}</span> {c.read_only && <span className="badge badge-ok">read-only</span>}
          </div></div>
        ))}
        <h3>Add connection</h3>
        <input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <select value={form.dialect} onChange={(e) => setForm({ ...form, dialect: e.target.value })} style={{ marginTop: 6 }}>
          <option value="sqlite">SQLite (file in project)</option>
          <option value="postgresql">PostgreSQL</option>
        </select>
        {form.dialect === "postgresql" && (
          <>
            <input placeholder="Host" style={{ marginTop: 6 }} value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} />
            <input placeholder="Port" style={{ marginTop: 6 }} value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} />
            <input placeholder="User" style={{ marginTop: 6 }} value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
            <input placeholder="Password (stored encrypted)" type="password" style={{ marginTop: 6 }} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </>
        )}
        <input placeholder={form.dialect === "sqlite" ? "Path inside project, e.g. data/app.db" : "Database name"} style={{ marginTop: 6 }} value={form.database} onChange={(e) => setForm({ ...form, database: e.target.value })} />
        <div className="field-inline" style={{ marginTop: 6 }}>
          <input type="checkbox" checked={form.read_only} onChange={(e) => setForm({ ...form, read_only: e.target.checked })} /> <span className="small">Read-only (blocks all writes)</span>
        </div>
        <button className="btn" style={{ marginTop: 8 }} disabled={create.busy || !form.name || !form.database} onClick={() => void create.run()}>Add</button>
        <ErrorAlert error={create.error || connections.error} />
      </div>
      <div className="card">
        {!selected ? <Empty>Add a database connection to query it. The SQL agent in chat can also generate queries.</Empty> : (
          <>
            <textarea className="mono" rows={6} value={query} onChange={(e) => setQuery(e.target.value)} />
            <div className="row" style={{ marginTop: 8 }}>
              <button className="btn btn-primary" disabled={run.busy} onClick={() => void run.run()}>Run</button>
              {validation && (validation.valid
                ? <span className={`badge ${validation.requires_approval ? "badge-warn" : "badge-ok"}`}>{validation.category}{validation.requires_approval ? " — needs approval" : ""}</span>
                : <span className="badge badge-danger">invalid: {validation.parse_error}</span>)}
              {validation?.warnings?.map((w: string) => <span key={w} className="badge badge-danger">{w}</span>)}
            </div>
            <ErrorAlert error={run.error} />
            {result && (
              <div style={{ marginTop: 10 }}>
                <div className="small">{result.summary}</div>
                {result.status === "approval_required" && <Link to="/approvals">Open approvals →</Link>}
                {result.data?.rows && (
                  <div style={{ overflowX: "auto", marginTop: 8 }}><table>
                    <thead><tr>{result.data.columns.map((c: string) => <th key={c}>{c}</th>)}</tr></thead>
                    <tbody>{result.data.rows.map((r: any, i: number) => <tr key={i}>{result.data.columns.map((c: string) => <td key={c} className="mono small">{String(r[c])}</td>)}</tr>)}</tbody>
                  </table></div>
                )}
              </div>
            )}
            <h3>Schema</h3>
            {schema.loading ? <Spinner /> : schema.data?.data?.tables ? (
              <div className="small mono">{Object.entries(schema.data.data.tables).map(([t, cols]: [string, any]) => <div key={t}><strong>{t}</strong>({cols.join(", ")})</div>)}</div>
            ) : <p className="muted small">{schema.data?.summary}</p>}
          </>
        )}
      </div>
    </div>
  );
}

export function DocumentsPanel({ projectId }: { projectId?: string }) {
  const docs = useAsync(() => api<any[]>("/documents", { query: { project_id: projectId } }), [projectId]);
  const upload = useAction(async (file: File) => {
    const form = new FormData();
    form.set("file", file);
    if (projectId) form.set("project_id", projectId);
    await api("/documents", { form, method: "POST" });
    await docs.reload();
  });
  const remove = useAction(async (id: string) => {
    await api(`/documents/${id}`, { method: "DELETE", allowEmpty: true });
    await docs.reload();
  });
  const processing = docs.data?.some((d) => ["queued", "processing"].includes(d.status));
  useEffect(() => {
    if (!processing) return;
    const id = setInterval(() => void docs.reload(), 2500);
    return () => clearInterval(id);
  }, [processing, docs]);
  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Documents</h3>
        <span className="spacer" />
        <input type="file" style={{ width: 280 }} accept=".md,.txt,.pdf,.docx,.sql,.json,.yaml,.yml,.rst,.csv,.log"
          onChange={(e) => e.target.files?.[0] && void upload.run(e.target.files[0])} />
      </div>
      <p className="muted small">Markdown, text, PDF, DOCX, SQL schema and notes are parsed, chunked, embedded and searchable with citations.</p>
      <ErrorAlert error={upload.error || docs.error || remove.error} />
      <table>
        <thead><tr><th>Title</th><th>Type</th><th>Status</th><th>Chunks</th><th /></tr></thead>
        <tbody>
          {docs.data?.map((d) => (
            <tr key={d.id}>
              <td>{d.title}</td><td>{d.doc_type}</td>
              <td><StatusBadge status={d.status} />{d.error && <div className="small muted">{d.error}</div>}</td>
              <td>{d.meta?.chunks ?? "—"}</td>
              <td><button className="btn btn-sm btn-danger" onClick={() => void remove.run(d.id)}>Delete</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
