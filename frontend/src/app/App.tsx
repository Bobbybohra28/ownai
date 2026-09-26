import { BrowserRouter, NavLink, Navigate, Outlet, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { Spinner } from "@/components/ui";
import Login from "@/features/Login";
import Dashboard from "@/features/Dashboard";
import Projects from "@/features/Projects";
import ProjectWorkspace from "@/features/ProjectWorkspace";
import Chat from "@/features/chat/Chat";
import Agents from "@/features/Agents";
import Models from "@/features/Models";
import Approvals from "@/features/Approvals";
import Rag from "@/features/Rag";
import MemoryPage from "@/features/Memory";
import Evaluation from "@/features/Evaluation";
import Settings from "@/features/Settings";
import Admin from "@/features/Admin";
import { ProjectScoped } from "@/features/ProjectScoped";

const NAV: [string, string][] = [
  ["/", "Dashboard"],
  ["/projects", "Projects"],
  ["/chat", "AI Chat"],
  ["/files", "Files"],
  ["/changes", "Changes"],
  ["/approvals", "Approvals"],
  ["/git", "Git"],
  ["/sql", "SQL / Data"],
  ["/rag", "RAG / Documents"],
  ["/memory", "Memory"],
];
const NAV_PLATFORM: [string, string][] = [
  ["/agents", "Agents"],
  ["/models", "Models"],
  ["/evaluation", "Evaluation"],
  ["/settings", "Settings"],
  ["/admin", "Admin"],
];

function Shell() {
  const { session, logout } = useAuth();
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-dot" /> OwnAI
        </div>
        <nav className="nav">
          <div className="nav-section">Workspace</div>
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              {label}
            </NavLink>
          ))}
          <div className="nav-section">Platform</div>
          {NAV_PLATFORM.map(([to, label]) => (
            <NavLink key={to} to={to}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div>{session?.user.display_name}</div>
          <div>
            {session?.organization.name} · {session?.role}
          </div>
          <button className="btn btn-sm" style={{ marginTop: 8 }} onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}

function Protected() {
  const { session, loading } = useAuth();
  if (loading)
    return (
      <div className="auth-page">
        <Spinner label="Loading…" />
      </div>
    );
  if (!session) return <Navigate to="/login" replace />;
  return <Shell />;
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route element={<Protected />}>
            <Route index element={<Dashboard />} />
            <Route path="projects" element={<Projects />} />
            <Route path="projects/:projectId" element={<ProjectWorkspace />} />
            <Route path="chat" element={<Chat />} />
            <Route path="chat/:conversationId" element={<Chat />} />
            <Route path="files" element={<ProjectScoped tab="files" title="Files" />} />
            <Route path="changes" element={<ProjectScoped tab="changes" title="Changes" />} />
            <Route path="git" element={<ProjectScoped tab="git" title="Git" />} />
            <Route path="sql" element={<ProjectScoped tab="sql" title="SQL / Data" />} />
            <Route path="approvals" element={<Approvals />} />
            <Route path="rag" element={<Rag />} />
            <Route path="memory" element={<MemoryPage />} />
            <Route path="agents" element={<Agents />} />
            <Route path="models" element={<Models />} />
            <Route path="evaluation" element={<Evaluation />} />
            <Route path="settings" element={<Settings />} />
            <Route path="admin" element={<Admin />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
