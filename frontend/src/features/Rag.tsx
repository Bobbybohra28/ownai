import { useState, type FormEvent } from "react";
import { api } from "@/api/client";
import type { Project } from "@/api/types";
import { ErrorAlert, Markdown, PageHeader, Spinner, useAction, useAsync } from "@/components/ui";
import { DocumentsPanel } from "./project/panels";

export default function Rag() {
  const projects = useAsync(() => api<Project[]>("/projects"));
  const [projectId, setProjectId] = useState("");
  const [query, setQuery] = useState("");
  const [answer, setAnswer] = useState(false);
  const [result, setResult] = useState<any>(null);
  const search = useAction(async () => setResult(await api("/rag/search", { body: { query, project_id: projectId || null, top_k: 8, answer } })));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void search.run();
  };
  return (
    <>
      <PageHeader title="RAG / Documents" />
      <form className="card" onSubmit={submit}>
        <div className="row">
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} style={{ width: 240 }} aria-label="Project">
            <option value="">All projects & documents</option>
            {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search code and documentation in natural language…" style={{ flex: 1 }} />
          <label className="field-inline" style={{ margin: 0 }}><input type="checkbox" checked={answer} onChange={(e) => setAnswer(e.target.checked)} /> answer</label>
          <button className="btn btn-primary" disabled={search.busy || query.length < 2}>{search.busy ? "Searching…" : "Search"}</button>
        </div>
      </form>
      <ErrorAlert error={search.error} />
      {search.busy && <Spinner label="Retrieving…" />}
      {result && (
        <div className="card">
          <div className="row small">
            <span className={`badge ${result.semantic ? "badge-ok" : "badge-warn"}`}>semantic {result.semantic ? "on" : "off"}</span>
            <span className={`badge ${result.lexical ? "badge-ok" : "badge-warn"}`}>keyword {result.lexical ? "on" : "off"}</span>
            <span className="badge">rerank {result.reranked ? "on" : "off"}</span>
          </div>
          {result.notices?.map((n: string) => <div key={n} className="alert alert-info small">{n}</div>)}
          {result.answer && (
            <div className="card" style={{ margin: "10px 0" }}>
              <strong>Answer</strong> {result.answer.models?.map((m: string) => <span key={m} className="badge badge-info">{m}</span>)}
              {result.answer.status === "succeeded" ? <Markdown text={result.answer.text} /> : <div className="alert alert-error">{result.answer.error}</div>}
            </div>
          )}
          {result.results.map((r: any, i: number) => (
            <div key={r.chunk_id} style={{ marginTop: 10 }}>
              <div className="row small"><strong>[S{i + 1}]</strong> <span className="mono">{r.file_path}:{r.start_line}-{r.end_line}</span> {r.symbol && <span className="badge">{r.symbol}</span>} <span className="muted">{r.sources.join(" + ")} · score {r.score}</span></div>
              <pre style={{ maxHeight: 220 }}>{r.content}</pre>
            </div>
          ))}
          {!result.results.length && <p className="muted">No matching content.</p>}
        </div>
      )}
      <div style={{ marginTop: 12 }}>
        <DocumentsPanel projectId={projectId || undefined} />
      </div>
    </>
  );
}
