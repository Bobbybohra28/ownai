import { useState } from "react";
import { api } from "@/api/client";
import { useAuth } from "@/app/auth";
import { ErrorAlert, PageHeader, useAction, useAsync } from "@/components/ui";

export default function Settings() {
  const { session, refresh } = useAuth();
  const [displayName, setDisplayName] = useState(session?.user.display_name ?? "");
  const [defaultMode, setDefaultMode] = useState(String(session?.user.preferences?.default_mode ?? "auto"));
  const [codingStyle, setCodingStyle] = useState(String(session?.user.preferences?.coding_style ?? ""));
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [memberEmail, setMemberEmail] = useState("");
  const [memberRole, setMemberRole] = useState("member");
  const members = useAsync(() => api<any[]>("/orgs/current/members"));
  const org = useAsync(() => api("/orgs/current"));
  const billing = useAsync(() => api("/billing/subscription"));
  const [saved, setSaved] = useState("");

  const saveProfile = useAction(async () => {
    await api("/users/me", { method: "PATCH", body: { display_name: displayName, preferences: { default_mode: defaultMode, coding_style: codingStyle } } });
    await refresh();
    setSaved("Profile saved.");
  });
  const changePassword = useAction(async () => {
    await api("/users/me/password", { body: { current_password: currentPw, new_password: newPw }, allowEmpty: true });
    setCurrentPw("");
    setNewPw("");
    setSaved("Password changed.");
  });
  const addMember = useAction(async () => {
    await api("/orgs/current/members", { body: { email: memberEmail, role: memberRole } });
    setMemberEmail("");
    await members.reload();
  });
  const removeMember = useAction(async (id: string) => {
    await api(`/orgs/current/members/${id}`, { method: "DELETE", allowEmpty: true });
    await members.reload();
  });

  return (
    <>
      <PageHeader title="Settings" />
      {saved && <div className="alert alert-info">{saved}</div>}
      <div className="grid grid-2">
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Profile & preferences</h2>
          <label>Display name</label>
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          <label>Default chat mode</label>
          <select value={defaultMode} onChange={(e) => setDefaultMode(e.target.value)}>
            {["auto", "quick", "developer", "deep", "project"].map((m) => <option key={m}>{m}</option>)}
          </select>
          <label>Coding style notes (shared with agents as a preference)</label>
          <textarea rows={3} value={codingStyle} onChange={(e) => setCodingStyle(e.target.value)} placeholder="e.g. type hints everywhere, small functions, pytest" />
          <button className="btn btn-primary" style={{ marginTop: 10 }} disabled={saveProfile.busy} onClick={() => void saveProfile.run()}>Save</button>
          <ErrorAlert error={saveProfile.error} />
          <h2>Password</h2>
          <input type="password" placeholder="Current password" value={currentPw} onChange={(e) => setCurrentPw(e.target.value)} autoComplete="current-password" />
          <input type="password" placeholder="New password" value={newPw} onChange={(e) => setNewPw(e.target.value)} style={{ marginTop: 6 }} autoComplete="new-password" />
          <button className="btn" style={{ marginTop: 8 }} disabled={changePassword.busy || !newPw} onClick={() => void changePassword.run()}>Change password</button>
          <ErrorAlert error={changePassword.error} />
        </div>
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Organization</h2>
          <p>{org.data?.name} · your role: <strong>{session?.role}</strong></p>
          <p className="small">Plan: <strong>{billing.data?.plan?.name}</strong> {billing.data?.billing_enabled ? "" : "(billing disabled — private deployment)"}</p>
          <p className="small muted">Usage this month: {Object.entries(billing.data?.usage ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"}</p>
          <h3>Members</h3>
          <table><tbody>{members.data?.map((m) => (
            <tr key={m.user_id}><td>{m.display_name}<div className="small muted">{m.email}</div></td><td>{m.role}</td>
              <td>{m.role !== "owner" && <button className="btn btn-sm btn-danger" onClick={() => void removeMember.run(m.user_id)}>Remove</button>}</td></tr>
          ))}</tbody></table>
          <div className="row" style={{ marginTop: 8 }}>
            <input placeholder="Email of registered user" value={memberEmail} onChange={(e) => setMemberEmail(e.target.value)} style={{ flex: 1 }} />
            <select value={memberRole} onChange={(e) => setMemberRole(e.target.value)} style={{ width: 120 }}>
              {["member", "admin", "viewer"].map((r) => <option key={r}>{r}</option>)}
            </select>
            <button className="btn" disabled={addMember.busy || !memberEmail} onClick={() => void addMember.run()}>Add</button>
          </div>
          <ErrorAlert error={addMember.error || removeMember.error || members.error} />
        </div>
      </div>
    </>
  );
}
