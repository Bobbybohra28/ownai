import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { Approval } from "@/api/types";
import { DiffView, Empty, ErrorAlert, PageHeader, Spinner, StatusBadge, Tabs, formatDate, useAction, useAsync } from "@/components/ui";

function ApprovalCard({ a, onDone }: { a: Approval; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [showDiff, setShowDiff] = useState(false);
  const diff = useAsync(async () => (showDiff && a.changeset && a.project_id ? api(`/projects/${a.project_id}/changesets/${a.changeset.id}`) : null), [showDiff]);
  const decide = useAction(async (approve: boolean) => {
    await api(`/approvals/${a.id}/${approve ? "approve" : "reject"}`, { body: { note } });
    onDone();
  });
  return (
    <div className="card">
      <div className="row">
        <strong>{a.title}</strong>
        <span className={`badge ${a.risk_level === "critical" ? "badge-danger" : "badge-warn"}`}>{a.risk_level} risk</span>
        <StatusBadge status={a.status} />
        <span className="spacer" />
        <span className="muted small">{formatDate(a.created_at)}</span>
      </div>
      <p className="small" style={{ whiteSpace: "pre-wrap" }}>{a.description}</p>
      {a.tool && <p className="small muted">Action: <span className="mono">{a.tool}</span> {a.run_id && <>· part of an agent run (it continues after your decision)</>}</p>}
      {a.changeset && (
        <p className="small">
          {a.changeset.stats.paths?.map((p) => <span key={p.path} className="badge" style={{ marginRight: 4 }}>{p.path} +{p.added}/-{p.removed}</span>)}
          <button className="btn btn-sm" onClick={() => setShowDiff(!showDiff)}>{showDiff ? "Hide diff" : "Show diff"}</button>
        </p>
      )}
      {showDiff && (diff.loading ? <Spinner /> : diff.data?.files?.map((f: any) => <div key={f.path}><div className="mono small">{f.path}</div><DiffView diff={f.diff} /></div>))}
      {a.tool === "run_sql" && <pre>{String(a.args.query ?? "")}</pre>}
      {a.status === "pending" && (
        <div className="row" style={{ marginTop: 8 }}>
          <input placeholder="Optional note" value={note} onChange={(e) => setNote(e.target.value)} style={{ maxWidth: 360 }} />
          <button className="btn btn-ok" disabled={decide.busy} onClick={() => void decide.run(true)}>Approve</button>
          <button className="btn btn-danger" disabled={decide.busy} onClick={() => void decide.run(false)}>Reject</button>
        </div>
      )}
      {a.result && Object.keys(a.result).length > 0 && <p className="small muted">Result: {String((a.result as any).summary ?? JSON.stringify(a.result))}</p>}
      <ErrorAlert error={decide.error} />
    </div>
  );
}

export default function Approvals() {
  const [tab, setTab] = useState<"pending" | "history">("pending");
  const list = useAsync(() => api<Approval[]>("/approvals", { query: { status: tab === "pending" ? "pending" : "" } }), [tab]);
  return (
    <>
      <PageHeader title="Approvals" />
      <p className="muted small">Risky actions — applying code changes, modifying SQL, commits, branch switches — never run without your explicit approval. <Link to="/changes">Review change sets →</Link></p>
      <Tabs<"pending" | "history"> tabs={[["pending", "Pending"], ["history", "History"]]} value={tab} onChange={setTab} />
      <ErrorAlert error={list.error} />
      {list.loading ? <Spinner /> : list.data?.length ? list.data.map((a) => <ApprovalCard key={a.id} a={a} onDone={() => void list.reload()} />) : <Empty>Nothing waiting for approval.</Empty>}
    </>
  );
}
