import React from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { LayoutDashboard, CalendarDays, Clock, Tags, Users, Settings as Cog, UserCircle, LogOut } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import { Wordmark } from "@/components/Brand";

const NAV = [
  { to: "/admin", icon: LayoutDashboard, label: "Dashboard", end: true },
  { to: "/admin/bookings", icon: CalendarDays, label: "Bookings" },
  { to: "/admin/availability", icon: Clock, label: "Availability" },
  { to: "/admin/appointment-types", icon: Tags, label: "Appointments" },
  { to: "/admin/admins", icon: Users, label: "Admins", superadmin: true },
  { to: "/admin/settings", icon: Cog, label: "Settings", superadmin: true },
  { to: "/admin/account", icon: UserCircle, label: "My Account" },
];

export default function AdminLayout() {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const items = NAV.filter((n) => !n.superadmin || user?.role === "superadmin");

  return (
    <div className="min-h-screen flex" style={{ background: "var(--ivory)" }}>
      <aside className="w-64 shrink-0 hidden md:flex flex-col border-r" style={{ borderColor: "var(--line)", background: "#fff" }}>
        <div className="p-6 border-b" style={{ borderColor: "var(--line)" }}>
          <Wordmark size="text-3xl" />
          <p className="eyebrow mt-1" style={{ fontSize: "0.5rem" }}>Appointments Admin</p>
        </div>
        <nav className="flex-1 p-4 space-y-1">
          {items.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} data-testid={`nav-${n.label.toLowerCase().replace(/\s/g, "-")}`}
              className={({ isActive }) =>
                `flex items-center gap-3 px-4 py-3 font-sans-j text-sm transition-colors ${isActive ? "" : "hover:bg-[var(--ivory-2)]"}`}
              style={({ isActive }) => ({
                background: isActive ? "var(--charcoal)" : "transparent",
                color: isActive ? "var(--ivory)" : "var(--ink)",
              })}>
              <n.icon size={17} /> {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="p-4 border-t" style={{ borderColor: "var(--line)" }}>
          <div className="px-2 mb-3">
            <p className="font-sans-j text-sm" style={{ color: "var(--charcoal)" }}>{user?.name}</p>
            <p className="eyebrow" style={{ fontSize: "0.5rem" }}>{user?.role}</p>
          </div>
          <button onClick={() => { logout(); nav("/admin/login"); }} data-testid="logout-btn"
            className="flex items-center gap-2 px-2 font-sans-j text-sm hover:text-[var(--gold-deep)]" style={{ color: "var(--taupe)" }}>
            <LogOut size={16} /> Sign Out
          </button>
        </div>
      </aside>

      {/* mobile top nav */}
      <div className="md:hidden fixed top-0 left-0 right-0 z-40 flex items-center justify-between px-4 py-3 border-b" style={{ borderColor: "var(--line)", background: "#fff" }}>
        <Wordmark size="text-2xl" />
        <button onClick={() => { logout(); nav("/admin/login"); }} style={{ color: "var(--taupe)" }} data-testid="logout-btn-mobile"><LogOut size={18} /></button>
      </div>

      <main className="flex-1 min-w-0 p-6 md:p-10 pt-20 md:pt-10 overflow-x-hidden">
        <div className="md:hidden flex gap-2 overflow-x-auto pb-4 mb-4">
          {items.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end}
              className="eyebrow whitespace-nowrap px-3 py-2 border" style={{ fontSize: "0.55rem", borderColor: "var(--line)" }}>
              {n.label}
            </NavLink>
          ))}
        </div>
        <Outlet />
      </main>
    </div>
  );
}
