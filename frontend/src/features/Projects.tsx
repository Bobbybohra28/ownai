import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import type { Project } from "@/api/types";
import { ErrorAlert, PageHeader, Spinner, StatusBadge, Tabs, formatDate, useAsync, usePolling } from "@/components/ui";

type Source = "zip" | "git" | "local" | "empty";

export default function Projects() {
  const projects = useAsync(() => api<Project[]>("/projects"));
  const navigate = useNavigate();
  const [source, setSource] = useState<Source>("zip");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [path, setPath] = useState("");
  const [link, setLink] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const busyProjects = projects.data?.some((p) => ["importing", "indexing"].includes(p.status)) ?? false;
  usePolling(() => void projects.reload(), 3000, busyProjects);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      let project: Project;
      if (source === "zip") {
        if (!file) throw new Error("Choose a .zip file of your project.");
        const form = new FormData();
        form.set("name", name);
        form.set("description", description);
        form.set("file", file);
        project = await api<Project>("/projects/import/upload", { form, method: "POST" });
      } else if (source === "git") {
        project = await api<Project>("/projects/import/git", { body: { name, description, url: gitUrl, branch: branch || null } });
      } else if (source === "local") {
        project = await api<Project>("/projects/import/local", { body: { name, description, path, link } });
      } else {
        project = await api<Project>("/projects", { body: { name, description } });
      }
      navigate(`/projects/${project.id}`);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHeader title="Projects" />
      <div className="grid grid-2">
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Your projects</h2>
          <ErrorAlert error={projects.error} />
          {projects.loading && !projects.data ? <Spinner /> : projects.data?.length ? (
            <table>
              <thead>
                <tr><th>Name</th><th>Source</th><th>Status</th><th>Indexed</th></tr>
              </thead>
              <tbody>
                {projects.data.map((p) => (
                  <tr key={p.id}>
                    <td><Link to={`/projects/${p.id}`}>{p.name}</Link><div className="muted small">{p.description}</div></td>
                    <td className="small">{p.source_type}{p.linked ? " (linked)" : ""}</td>
                    <td><StatusBadge status={p.status} /><div className="muted small">{p.status_message}</div></td>
                    <td className="small muted">{formatDate(p.indexed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="muted">No projects yet.</p>}
        </div>
        <form className="card" onSubmit={submit}>
          <h2 style={{ marginTop: 0 }}>Create or import</h2>
          <Tabs<Source> tabs={[["zip", "Upload .zip"], ["git", "Git URL"], ["local", "Local folder"], ["empty", "Empty"]]} value={source} onChange={setSource} />
          <label htmlFor="pname">Project name</label>
          <input id="pname" required value={name} onChange={(e) => setName(e.target.value)} />
          <label htmlFor="pdesc">Description</label>
          <input id="pdesc" value={description} onChange={(e) => setDescription(e.target.value)} />
          {source === "zip" && (
            <>
              <label htmlFor="pzip">Archive (.zip)</label>
              <input id="pzip" type="file" accept=".zip" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            </>
          )}
          {source === "git" && (
            <>
              <label htmlFor="pgit">Repository URL (https or ssh)</label>
              <input id="pgit" required value={gitUrl} onChange={(e) => setGitUrl(e.target.value)} placeholder="https://github.com/org/repo.git" />
              <label htmlFor="pbranch">Branch (optional)</label>
              <input id="pbranch" value={branch} onChange={(e) => setBranch(e.target.value)} />
            </>
          )}
          {source === "local" && (
            <>
              <label htmlFor="ppath">Folder path on the server</label>
              <input id="ppath" required value={path} onChange={(e) => setPath(e.target.value)} placeholder="/workspace/my-app" />
              <div className="field-inline" style={{ marginTop: 8 }}>
                <input id="plink" type="checkbox" checked={link} onChange={(e) => setLink(e.target.checked)} />
                <label htmlFor="plink" style={{ margin: 0 }}>Work on the folder in place (approved changes are written there)</label>
              </div>
              <p className="muted small">Only folders under OWNAI_LOCAL_IMPORT_ROOTS are allowed.</p>
            </>
          )}
          <ErrorAlert error={error} />
          <button className="btn btn-primary" style={{ marginTop: 12 }} disabled={busy}>
            {busy ? "Working…" : source === "empty" ? "Create project" : "Import project"}
          </button>
          <p className="muted small">Credential files (.env, keys) are never indexed or sent to models; secrets in code are masked.</p>
        </form>
      </div>
    </>
  );
}
