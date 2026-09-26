import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { Project } from "@/api/types";
import { ErrorAlert, PageHeader, Spinner, StatusBadge, Tabs, useAction, useAsync, usePolling } from "@/components/ui";
import { ChangesPanel, DocumentsPanel, FilesPanel, GitPanel, OverviewPanel, SqlPanel } from "./project/panels";

export type WorkspaceTab = "overview" | "files" | "changes" | "git" | "sql" | "documents";

export function ProjectPanel({ project, tab }: { project: Project; tab: WorkspaceTab }) {
  switch (tab) {
    case "files":
      return <FilesPanel project={project} />;
    case "changes":
      return <ChangesPanel project={project} />;
    case "git":
      return <GitPanel project={project} />;
    case "sql":
      return <SqlPanel project={project} />;
    case "documents":
      return <DocumentsPanel projectId={project.id} />;
    default:
      return <OverviewPanel project={project} />;
  }
}

export default function ProjectWorkspace() {
  const { projectId } = useParams();
  const navigate = useNavigate();
  const project = useAsync(() => api<Project>(`/projects/${projectId}`), [projectId]);
  const [tab, setTab] = useState<WorkspaceTab>("overview");
  const indexing = ["importing", "indexing"].includes(project.data?.status ?? "");
  usePolling(() => void project.reload(), 2500, indexing);

  const reindex = useAction(async () => {
    await api(`/projects/${projectId}/reindex`, { method: "POST" });
    await project.reload();
  });
  const startChat = useAction(async () => {
    const conv = await api("/conversations", { body: { project_id: projectId, title: `${project.data?.name}`, mode: "auto" } });
    navigate(`/chat/${conv.id}`);
  });
  const remove = useAction(async () => {
    if (!window.confirm(`Delete project "${project.data?.name}"? Imported copies are removed; linked folders are left untouched.`)) return;
    await api(`/projects/${projectId}`, { method: "DELETE", allowEmpty: true });
    navigate("/projects");
  });

  if (project.loading && !project.data) return <Spinner label="Loading project…" />;
  if (project.error) return <ErrorAlert error={project.error} />;
  const p = project.data!;
  return (
    <>
      <PageHeader
        title={p.name}
        actions={
          <>
            <StatusBadge status={p.status} />
            <button className="btn" onClick={() => void reindex.run()} disabled={reindex.busy || indexing}>Re-index</button>
            <button className="btn btn-primary" onClick={() => void startChat.run()} disabled={p.status !== "ready"}>Chat about this project</button>
            <button className="btn btn-danger" onClick={() => void remove.run()}>Delete</button>
          </>
        }
      />
      {indexing && <div className="alert alert-info"><Spinner label={p.status_message || "Indexing…"} /></div>}
      {p.status === "error" && <div className="alert alert-error">{p.status_message}</div>}
      <ErrorAlert error={reindex.error || startChat.error || remove.error} />
      <p className="muted small">
        Source: {p.source_type} {p.source_ref && <span className="mono">{p.source_ref}</span>} {p.linked && "(linked folder — approved changes are written there)"} · <Link to="/projects">All projects</Link>
      </p>
      <Tabs<WorkspaceTab>
        tabs={[["overview", "Overview"], ["files", "Files"], ["changes", "Changes"], ["git", "Git"], ["sql", "SQL / Data"], ["documents", "Documents"]]}
        value={tab}
        onChange={setTab}
      />
      <ProjectPanel project={p} tab={tab} />
    </>
  );
}
