import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "@/api/client";
import { streamRun } from "@/api/sse";
import type { Conversation, Message, Project } from "@/api/types";
import { ErrorAlert, Markdown, Spinner, StatusBadge, useAction, useAsync } from "@/components/ui";
import { initialRun, reduceRun, type LiveRun } from "./runState";

const MODES: [string, string][] = [
  ["auto", "Auto"],
  ["quick", "Quick"],
  ["developer", "Developer"],
  ["deep", "Deep analysis"],
  ["project", "Project"],
  ["debug", "Debug"],
  ["review", "Review"],
  ["architecture", "Architecture"],
];

function LiveRunPanel({ run, onCancel }: { run: LiveRun; onCancel: () => void }) {
  const current = run.messages[run.messages.length - 1];
  return (
    <div className="msg msg-assistant" data-testid="live-run">
      <div className="msg-meta">
        <StatusBadge status={run.status} />
        {run.intent && <span className="badge">{run.intent.intent} · {run.intent.mode}</span>}
        {run.models.map((m) => <span key={m} className="badge badge-info">model: {m}</span>)}
        {!run.done && <button className="btn btn-sm" onClick={onCancel}>Cancel</button>}
      </div>
      <div className="bubble">
        {!run.done && <Spinner label={current ?? "Starting…"} />}
        <div className="progress">
          {run.order.map((key) => {
            const st = run.steps[key];
            return (
              <div key={key} className="progress-line">
                <StatusBadge status={st.status} /> <strong>{st.agentName ?? st.agent}</strong>
                {st.models.length > 0 && <span className="muted"> · {st.models.join(", ")}</span>}
                {st.durationMs != null && <span className="muted"> · {(st.durationMs / 1000).toFixed(1)}s</span>}
                {st.summary && <div className="small muted">{st.summary}</div>}
                {st.error && <div className="small" style={{ color: "var(--danger)" }}>{st.error}</div>}
                {st.tools.map((t, i) => (
                  <div key={i} className="small"><span className="mono">{t.tool}</span> <StatusBadge status={t.status === "ok" ? "ok" : t.status} /> <span className="muted">{t.summary}</span></div>
                ))}
              </div>
            );
          })}
          {run.verification.map((v, i) => (
            <div key={i} className="progress-line small">
              verify/{v.source}: <StatusBadge status={v.verdict ?? v.status ?? "info"} /> {v.summary}
            </div>
          ))}
        </div>
        {run.notices.map((n, i) => <div key={i} className={`alert ${n.level === "error" ? "alert-error" : n.level === "warning" ? "alert-warn" : "alert-info"} small`}>{n.message}</div>)}
        {run.streamed && <Markdown text={run.streamed} />}
        {run.error && <div className="alert alert-error">{run.error.message}{run.error.hint && <div className="small">{run.error.hint}</div>}</div>}
      </div>
    </div>
  );
}

function ApprovalActions({ report, onDecided }: { report: any; onDecided: () => void }) {
  const approvals: any[] = report?.approvals ?? [];
  const decide = useAction(async (id: string, approve: boolean) => {
    await api(`/approvals/${id}/${approve ? "approve" : "reject"}`, { body: {} });
    onDecided();
  });
  const pending = approvals.filter((a) => a.status === "pending");
  if (!pending.length) return null;
  return (
    <div className="alert alert-warn">
      {pending.map((a) => (
        <div key={a.id} className="row">
          <strong>{a.title}</strong> <span className="badge badge-warn">{a.risk_level} risk</span>
          <span className="spacer" />
          {report?.changeset?.id && <Link to="/changes">Review diff</Link>}
          <button className="btn btn-ok btn-sm" disabled={decide.busy} onClick={() => void decide.run(a.id, true)}>Approve</button>
          <button className="btn btn-danger btn-sm" disabled={decide.busy} onClick={() => void decide.run(a.id, false)}>Reject</button>
        </div>
      ))}
      <ErrorAlert error={decide.error} />
    </div>
  );
}

function AssistantMessage({ message, onDecided }: { message: Message; onDecided: () => void }) {
  const report = message.meta?.report;
  const failed = !!message.meta?.error;
  return (
    <div className="msg msg-assistant" data-testid="assistant-message" data-status={failed ? "failed" : message.meta?.status ?? "final"}>
      <div className="msg-meta">
        {failed ? <StatusBadge status="failed" /> : message.meta?.status === "awaiting_approval" ? <StatusBadge status="awaiting_approval" /> : null}
        {report?.primary_agent && <span className="badge">answered by {report.primary_agent}</span>}
        {(report?.agents ?? []).map((a: string) => <span key={a} className="badge">{a}</span>)}
        {(report?.models ?? []).map((m: string) => <span key={m} className="badge badge-info">{m}</span>)}
        {report?.verification?.verdict && <span className="badge">critic: <StatusBadge status={report.verification.verdict} /></span>}
      </div>
      <div className="bubble" style={failed ? { borderColor: "var(--danger)" } : undefined}>
        <Markdown text={message.content} />
        {message.meta?.status === "awaiting_approval" && <ApprovalActions report={report} onDecided={onDecided} />}
      </div>
    </div>
  );
}

export default function Chat() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const conversations = useAsync(() => api<Conversation[]>("/conversations"), []);
  const projects = useAsync(() => api<Project[]>("/projects"), []);
  const conversation = useAsync(async () => (conversationId ? api<Conversation>(`/conversations/${conversationId}`) : null), [conversationId]);
  const messages = useAsync(async () => (conversationId ? api<Message[]>(`/conversations/${conversationId}/messages`) : []), [conversationId]);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState("auto");
  const [live, setLive] = useState<LiveRun | null>(null);
  const [error, setError] = useState<unknown>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (conversation.data) setMode(conversation.data.mode);
  }, [conversation.data]);

  useEffect(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), [messages.data, live?.streamed, live?.order.length]);

  const attach = useCallback((runId: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLive(initialRun(runId));
    void streamRun(runId, (event) => setLive((prev) => (prev ? reduceRun(prev, event) : prev)), controller.signal).then(async () => {
      if (controller.signal.aborted) return;
      await messages.reload();
      await conversations.reload();
      setLive((prev) => (prev && prev.error ? prev : null));
    });
  }, [messages, conversations]);

  // re-attach to a run that is still in progress (e.g. after a page reload or an approval)
  useEffect(() => {
    const last = [...(messages.data ?? [])].reverse().find((m) => m.run_id);
    if (!last?.run_id || live) return;
    void api(`/runs/${last.run_id}`).then((run) => {
      if (["queued", "running"].includes(run.status)) attach(last.run_id!);
    }).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages.data]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const newConversation = async (projectId: string | null) => {
    const conv = await api<Conversation>("/conversations", { body: { project_id: projectId, mode: "auto" } });
    await conversations.reload();
    navigate(`/chat/${conv.id}`);
  };

  const send = async () => {
    const content = input.trim();
    if (!content || !conversationId) return;
    setError(null);
    try {
      const result = await api(`/conversations/${conversationId}/messages`, { body: { content, mode } });
      setInput("");
      await messages.reload();
      attach(result.run_id);
    } catch (e) {
      setError(e);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send();
  };

  const updateConversation = async (patch: Record<string, unknown>) => {
    if (!conversationId) return;
    await api(`/conversations/${conversationId}`, { method: "PATCH", body: patch });
    await conversation.reload();
  };

  const onDecided = async () => {
    await messages.reload();
    const last = [...(messages.data ?? [])].reverse().find((m) => m.run_id);
    if (last?.run_id) setTimeout(() => attach(last.run_id!), 300);
  };

  const project = projects.data?.find((p) => p.id === conversation.data?.project_id);
  return (
    <div className="chat">
      <div className="card chat-list">
        <div className="row" style={{ marginBottom: 8 }}>
          <button className="btn btn-primary btn-sm" onClick={() => void newConversation(null)}>New chat</button>
        </div>
        {projects.data?.filter((p) => p.status === "ready").slice(0, 5).map((p) => (
          <button key={p.id} className="btn btn-sm" style={{ width: "100%", marginBottom: 4, textAlign: "left" }} onClick={() => void newConversation(p.id)}>
            + chat about {p.name}
          </button>
        ))}
        <div className="nav-section" style={{ padding: "8px 0 4px" }}>Conversations</div>
        {conversations.data?.map((c) => (
          <Link key={c.id} to={`/chat/${c.id}`} className={c.id === conversationId ? "active" : ""} title={c.title}>{c.title}</Link>
        ))}
      </div>
      <div className="chat-main">
        {!conversationId ? (
          <div className="empty">
            <h2>Ask OwnAI anything about code</h2>
            <p>Start a chat, or pick a project so agents can use its code, tests and documentation.</p>
          </div>
        ) : (
          <>
            <div className="row" style={{ marginBottom: 8 }}>
              <strong>{conversation.data?.title}</strong>
              <span className="spacer" />
              <select style={{ width: 220 }} value={conversation.data?.project_id ?? ""} aria-label="Project"
                onChange={(e) => void updateConversation({ project_id: e.target.value || null })}>
                <option value="">No project</option>
                {projects.data?.map((p) => <option key={p.id} value={p.id} disabled={p.status !== "ready"}>{p.name} {p.status !== "ready" ? `(${p.status})` : ""}</option>)}
              </select>
              <select style={{ width: 160 }} value={mode} aria-label="Mode" onChange={(e) => { setMode(e.target.value); void updateConversation({ mode: e.target.value }); }}>
                {MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
            <div className="chat-messages">
              {messages.loading && !messages.data ? <Spinner /> : null}
              <ErrorAlert error={messages.error} />
              {messages.data?.map((m) =>
                m.role === "user" ? (
                  <div key={m.id} className="msg msg-user"><div className="bubble">{m.content}</div></div>
                ) : (
                  <AssistantMessage key={m.id} message={m} onDecided={onDecided} />
                ),
              )}
              {live && <LiveRunPanel run={live} onCancel={() => void api(`/runs/${live.runId}/cancel`, { method: "POST" })} />}
              <div ref={bottomRef} />
            </div>
            <div className="composer">
              <ErrorAlert error={error} />
              <textarea
                placeholder={project ? `Ask about ${project.name}… e.g. "Explain how authentication works" or "Find the bug in login and fix it"` : "Ask a programming question… (Ctrl+Enter to send)"}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKey}
                disabled={!!live && !live.done}
              />
              <div className="row" style={{ marginTop: 6 }}>
                <span className="muted small">
                  {project ? <>Project: <Link to={`/projects/${project.id}`}>{project.name}</Link></> : "No project context"} · Ctrl+Enter to send · risky actions always ask for your approval
                </span>
                <span className="spacer" />
                <button className="btn btn-primary" onClick={() => void send()} disabled={!input.trim() || (!!live && !live.done)}>Send</button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
