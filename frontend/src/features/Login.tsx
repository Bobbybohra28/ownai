import { useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "@/app/auth";
import { ErrorAlert } from "@/components/ui";

export default function Login() {
  const { session, login, register } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  if (session) return <Navigate to="/" replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") await login(email, password);
      else await register(email, password, name);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page">
      <form className="card auth-card" onSubmit={submit}>
        <div className="brand" style={{ padding: 0, marginBottom: 12 }}>
          <span className="brand-dot" /> OwnAI
        </div>
        <p className="muted small">Private multi-agent AI developer platform</p>
        {mode === "register" && (
          <>
            <label htmlFor="name">Name</label>
            <input id="name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
          </>
        )}
        <label htmlFor="email">Email</label>
        <input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === "login" ? "current-password" : "new-password"}
        />
        {mode === "register" && <p className="muted small">At least 10 characters, mixing letters with digits or symbols.</p>}
        <ErrorAlert error={error} />
        <button className="btn btn-primary" style={{ width: "100%", marginTop: 14 }} disabled={busy}>
          {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
        </button>
        <p className="small muted" style={{ marginTop: 12 }}>
          {mode === "login" ? "No account yet? " : "Already registered? "}
          <a href="#" onClick={(e) => { e.preventDefault(); setMode(mode === "login" ? "register" : "login"); setError(null); }}>
            {mode === "login" ? "Create one" : "Sign in"}
          </a>
        </p>
      </form>
    </div>
  );
}
