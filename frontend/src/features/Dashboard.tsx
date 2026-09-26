import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { Approval, Project } from "@/api/types";
import { ErrorAlert, PageHeader, Spinner, StatusBadge, formatDate, useAsync } from "@/components/ui";

export default function Dashboard() {
  const health = useAsync(() => fetch("/health").then((r) => r.json()));
  const projects = useAsync(() => api<Project[]>("/projects"));
  const approvals = useAsync(() => api<Approval[]>("/approvals"));
  const runs = useAsync(() => api<any[]>("/runs", { query: { limit: 8 } }));
  const models = useAsync(() => api<{ models: any[] }>("/models"));

  const checks = health.data?.checks ?? {};
  return (
    <>
      <PageHeader title="Dashboard" actions={<Link className="btn btn-primary" to="/chat">New chat</Link>} />
      <div className="grid grid-4">
        <div className="card">
          <div className="muted small">Models online</div>
          <div className="stat">{models.data ? `${models.data.models.filter((m) => m.online).length}/${models.data.models.length}` : "…"}</div>
          <Link className="small" to="/models">Model monitor →</Link>
        </div>
        <div className="card">
          <div className="muted small">Projects</div>
          <div className="stat">{projects.data?.length ?? "…"}</div>
          <Link className="small" to="/projects">Manage →</Link>
        </div>
        <div className="card">
          <div className="muted small">Pending approvals</div>
          <div className="stat">{approvals.data?.length ?? "…"}</div>
          <Link className="small" to="/approvals">Review →</Link>
        </div>
        <div className="card">
          <div className="muted small">Services</div>
          {health.loading ? <Spinner /> : (
            <div className="row" style={{ marginTop: 6 }}>
              {Object.entries(checks).map(([name, c]: [string, any]) => (
                <span key={name} className={`badge ${c.ok ? "badge-ok" : "badge-danger"}`}>{name}</span>
              ))}
            </div>
          )}
        </div>
      </div>
      <ErrorAlert error={health.error || projects.error} />
      <div className="grid grid-2" style={{ marginTop: 12 }}>
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Recent tasks</h2>
          {runs.loading ? <Spinner /> : runs.data?.length ? (
            <table>
              <tbody>
                {runs.data.map((r) => (
                  <tr key={r.id}>
                    <td>{r.conversation_id ? <Link to={`/chat/${r.conversation_id}`}>{r.request.slice(0, 70)}</Link> : r.request.slice(0, 70)}</td>
                    <td><StatusBadge status={r.status} /></td>
                    <td className="muted small">{formatDate(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="muted">No tasks yet — start a chat.</p>}
        </div>
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Projects</h2>
          {projects.data?.length ? (
            <table>
              <tbody>
                {projects.data.slice(0, 8).map((p) => (
                  <tr key={p.id}>
                    <td><Link to={`/projects/${p.id}`}>{p.name}</Link></td>
                    <td><StatusBadge status={p.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="muted">No projects yet. <Link to="/projects">Import one</Link>.</p>}
        </div>
      </div>
    </>
  );
}
