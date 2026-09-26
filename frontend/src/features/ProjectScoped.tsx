import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { Project } from "@/api/types";
import { ErrorAlert, PageHeader, Spinner, useAsync } from "@/components/ui";
import { ProjectPanel, type WorkspaceTab } from "./ProjectWorkspace";

const STORAGE_KEY = "ownai.selectedProject";

/** Global pages (Files, Changes, Git, SQL) that operate on a selected project. */
export function ProjectScoped({ tab, title }: { tab: WorkspaceTab; title: string }) {
  const projects = useAsync(() => api<Project[]>("/projects"));
  const [projectId, setProjectId] = useState<string>(() => localStorage.getItem(STORAGE_KEY) ?? "");
  const project = useAsync(async () => (projectId ? api<Project>(`/projects/${projectId}`) : null), [projectId]);

  useEffect(() => {
    if (projects.data?.length && !projects.data.some((p) => p.id === projectId)) setProjectId(projects.data[0].id);
  }, [projects.data, projectId]);
  useEffect(() => {
    if (projectId) localStorage.setItem(STORAGE_KEY, projectId);
  }, [projectId]);

  return (
    <>
      <PageHeader
        title={title}
        actions={
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} style={{ width: 260 }} aria-label="Project">
            {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        }
      />
      <ErrorAlert error={projects.error || project.error} />
      {projects.data && projects.data.length === 0 && <p className="muted">No projects yet. <Link to="/projects">Import one</Link>.</p>}
      {project.loading ? <Spinner /> : project.data && <ProjectPanel project={project.data} tab={tab} />}
    </>
  );
}
