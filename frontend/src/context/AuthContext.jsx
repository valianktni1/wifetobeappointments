import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
import api, { apiErr } from "@/lib/api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null); // null = checking, false = anon, obj = user
  const [checking, setChecking] = useState(true);

  const refresh = useCallback(async () => {
    const token = localStorage.getItem("wtb_token");
    if (!token) { setUser(false); setChecking(false); return; }
    try {
      const { data } = await api.get("/auth/me");
      setUser(data);
    } catch (e) {
      localStorage.removeItem("wtb_token");
      setUser(false);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const login = async (email, password) => {
    const { data } = await api.post("/auth/login", { email, password });
    if (data.mfa_required) return { mfa_required: true, mfa_token: data.mfa_token };
    localStorage.setItem("wtb_token", data.access_token);
    setUser(data.user);
    return { ok: true };
  };

  const verify2fa = async (mfa_token, code) => {
    const { data } = await api.post("/auth/2fa/verify", { mfa_token, code });
    localStorage.setItem("wtb_token", data.access_token);
    setUser(data.user);
    return { ok: true };
  };

  const logout = () => {
    localStorage.removeItem("wtb_token");
    setUser(false);
  };

  return (
    <AuthContext.Provider value={{ user, checking, login, verify2fa, logout, refresh, setUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
export { apiErr };
