import "@/App.css";
import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import Booking from "@/pages/Booking";
import ManageBooking from "@/pages/ManageBooking";
import Login from "@/pages/admin/Login";
import AdminLayout from "@/pages/admin/AdminLayout";
import Dashboard from "@/pages/admin/Dashboard";
import Bookings from "@/pages/admin/Bookings";
import Availability from "@/pages/admin/Availability";
import AppointmentTypes from "@/pages/admin/AppointmentTypes";
import Admins from "@/pages/admin/Admins";
import Settings from "@/pages/admin/Settings";
import Account from "@/pages/admin/Account";
import Customise from "@/pages/admin/Customise";
import Waitlist from "@/pages/admin/Waitlist";

function Protected({ children }) {
  const { user, checking } = useAuth();
  if (checking || user === null)
    return <div className="min-h-screen flex items-center justify-center" style={{ background: "var(--ivory)" }}>
      <span className="eyebrow">Loading…</span></div>;
  if (!user) return <Navigate to="/admin/login" replace />;
  return children;
}

function App() {
  return (
    <div className="App">
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<Booking />} />
            <Route path="/booking/:reference" element={<ManageBooking />} />
            <Route path="/admin/login" element={<Login />} />
            <Route path="/admin" element={<Protected><AdminLayout /></Protected>}>
              <Route index element={<Dashboard />} />
              <Route path="bookings" element={<Bookings />} />
              <Route path="availability" element={<Availability />} />
              <Route path="appointment-types" element={<AppointmentTypes />} />
              <Route path="customise" element={<Customise />} />
              <Route path="waitlist" element={<Waitlist />} />
              <Route path="admins" element={<Admins />} />
              <Route path="settings" element={<Settings />} />
              <Route path="account" element={<Account />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
        <Toaster position="top-center" />
      </AuthProvider>
    </div>
  );
}

export default App;
