import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, onAuthChange, refreshSession, setAccessToken } from "@/api/client";
import type { Session } from "@/api/types";

interface AuthState {
  session: Session | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, displayName: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  const loadMe = useCallback(async () => {
    const me = await api<Session>("/auth/me");
    setSession(me);
  }, []);

  useEffect(() => {
    (async () => {
      if (await refreshSession()) {
        try {
          await loadMe();
        } catch {
          setSession(null);
        }
      }
      setLoading(false);
    })();
    return onAuthChange((authenticated) => {
      if (!authenticated) setSession(null);
    });
  }, [loadMe]);

  const handleTokens = useCallback((data: any) => {
    setAccessToken(data.access_token);
    setSession({ user: data.user, organization: data.organization, role: data.role });
  }, []);

  const value: AuthState = {
    session,
    loading,
    login: async (email, password) => handleTokens(await api("/auth/login", { body: { email, password } })),
    register: async (email, password, display_name) =>
      handleTokens(await api("/auth/register", { body: { email, password, display_name } })),
    logout: async () => {
      await api("/auth/logout", { method: "POST", allowEmpty: true }).catch(() => undefined);
      setAccessToken(null);
      setSession(null);
    },
    refresh: loadMe,
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
