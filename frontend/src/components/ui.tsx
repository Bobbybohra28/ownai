import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { errorMessage } from "@/api/client";

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="row">
      <span className="spinner" aria-hidden /> {label && <span className="muted small">{label}</span>}
    </span>
  );
}

export function ErrorAlert({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="alert alert-error" role="alert">
      {errorMessage(error)}
    </div>
  );
}

const STATUS_CLASS: Record<string, string> = {
  online: "badge-ok", ready: "badge-ok", succeeded: "badge-ok", completed: "badge-ok", approved: "badge-ok", executed: "badge-ok",
  applied: "badge-ok", passed: "badge-ok", pass: "badge-ok", ok: "badge-ok",
  degraded: "badge-warn", awaiting_approval: "badge-warn", pending: "badge-warn", indexing: "badge-info", importing: "badge-info",
  running: "badge-info", queued: "badge-info", open: "badge-info", uncertain: "badge-warn", completed_with_warnings: "badge-warn",
  offline: "badge-danger", failed: "badge-danger", error: "badge-danger", rejected: "badge-danger", fail: "badge-danger",
  cancelled: "badge-danger", conflict: "badge-danger", expired: "badge-danger",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  const value = status ?? "unknown";
  return <span className={`badge ${STATUS_CLASS[value] ?? ""}`}>{value.replace(/_/g, " ")}</span>;
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function PageHeader({ title, actions }: { title: string; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <h1>{title}</h1>
      <div className="row">{actions}</div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: [T, string][]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map(([key, label]) => (
        <button key={key} role="tab" aria-selected={value === key} className={value === key ? "active" : ""} onClick={() => onChange(key)}>
          {label}
        </button>
      ))}
    </div>
  );
}

export function DiffView({ diff }: { diff: string }) {
  if (!diff) return <div className="muted small">No differences.</div>;
  return (
    <div className="diff">
      {diff.split("\n").map((line, i) => {
        const cls = line.startsWith("+++") || line.startsWith("---") ? "" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "";
        return (
          <span key={i} className={cls} style={cls ? undefined : { display: "block" }}>
            {line || " "}
          </span>
        );
      })}
    </div>
  );
}

/** Data loader hook with loading/error state and reload. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | undefined>();
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await fnRef.current());
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload, setData };
}

/** Wraps an async action with busy/error state. */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = useCallback(
    async (...args: A): Promise<R | undefined> => {
      setBusy(true);
      setError(null);
      try {
        return await fn(...args);
      } catch (e) {
        setError(e);
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [fn],
  );
  return { run, busy, error, setError };
}

export function formatDate(value: string | null | undefined) {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

export function usePolling(callback: () => void, intervalMs: number, active: boolean) {
  useEffect(() => {
    if (!active) return;
    const id = setInterval(callback, intervalMs);
    return () => clearInterval(id);
  }, [callback, intervalMs, active]);
}
