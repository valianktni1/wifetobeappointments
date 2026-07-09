import React, { useEffect, useState, useCallback } from "react";
import { format } from "date-fns";
import { toast } from "sonner";
import api, { apiErr } from "@/lib/api";
import { PageHead, Panel, Modal, Field, StatusBadge } from "@/components/admin/ui";

export default function Bookings() {
  const [shops, setShops] = useState([]);
  const [rows, setRows] = useState([]);
  const [filters, setFilters] = useState({ shop_id: "", status: "" });
  const [active, setActive] = useState(null);
  const [resched, setResched] = useState({ date: "", start_time: "", admin_notes: "" });

  const load = useCallback(async () => {
    const params = {};
    if (filters.shop_id) params.shop_id = filters.shop_id;
    if (filters.status) params.status = filters.status;
    try { const { data } = await api.get("/bookings", { params }); setRows(data); } catch (e) { toast.error(apiErr(e)); }
  }, [filters]);

  useEffect(() => { api.get("/shops").then((r) => setShops(r.data)); }, []);
  useEffect(() => { load(); }, [load]);

  const setStatus = async (b, status) => {
    try { await api.patch(`/bookings/${b.id}`, { status }); toast.success(`Marked ${status}`); load(); if (active?.id === b.id) setActive({ ...active, status }); }
    catch (e) { toast.error(apiErr(e)); }
  };

  const openDetail = (b) => { setActive(b); setResched({ date: b.date, start_time: b.start_time, admin_notes: b.admin_notes || "" }); };

  const exportCsv = async () => {
    try {
      const params = {};
      if (filters.shop_id) params.shop_id = filters.shop_id;
      if (filters.status) params.status = filters.status;
      const res = await api.get("/bookings/export.csv", { params, responseType: "blob" });
      const url = URL.createObjectURL(res.data);
      const a = document.createElement("a");
      a.href = url; a.download = "wifetobe-bookings.csv"; a.click();
      URL.revokeObjectURL(url);
    } catch (e) { toast.error(apiErr(e)); }
  };

  const saveResched = async () => {
    try {
      await api.patch(`/bookings/${active.id}`, resched);
      toast.success("Booking updated"); setActive(null); load();
    } catch (e) { toast.error(apiErr(e)); }
  };

  return (
    <div className="reveal-up">
      <PageHead eyebrow="Manage" title="Bookings">
        <button className="btn-wtb btn-ghost-wtb" onClick={exportCsv} data-testid="export-csv">Export CSV</button>
      </PageHead>
      <Panel className="mb-6">
        <div className="flex flex-wrap gap-4 items-end">
          <Field label="Boutique">
            <select className="input-wtb" value={filters.shop_id} data-testid="filter-shop"
              onChange={(e) => setFilters({ ...filters, shop_id: e.target.value })}>
              <option value="">All boutiques</option>
              {shops.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </Field>
          <Field label="Status">
            <select className="input-wtb" value={filters.status} data-testid="filter-status"
              onChange={(e) => setFilters({ ...filters, status: e.target.value })}>
              <option value="">All statuses</option>
              {["pending", "confirmed", "completed", "cancelled"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </Field>
        </div>
      </Panel>

      <div className="card-wtb overflow-x-auto" data-testid="bookings-table">
        <table className="w-full text-sm font-sans-j">
          <thead>
            <tr className="text-left" style={{ background: "var(--ivory-2)" }}>
              {["Date", "Time", "Customer", "Boutique", "Appointment", "Status", ""].map((h) => (
                <th key={h} className="field-label p-4">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={7} className="p-6 text-center" style={{ color: "var(--taupe)" }}>No bookings found.</td></tr>}
            {rows.map((b) => (
              <tr key={b.id} className="border-t hover:bg-[var(--ivory-2)] cursor-pointer" style={{ borderColor: "var(--line)" }}
                onClick={() => openDetail(b)} data-testid={`booking-row-${b.reference}`}>
                <td className="p-4 whitespace-nowrap">{format(new Date(b.date), "EEE d MMM")}</td>
                <td className="p-4" style={{ color: "var(--gold-deep)" }}>{b.start_time}</td>
                <td className="p-4">{b.customer_name}</td>
                <td className="p-4">{b.shop_name}</td>
                <td className="p-4">{b.appointment_type_name}</td>
                <td className="p-4"><StatusBadge status={b.status} /></td>
                <td className="p-4 text-right eyebrow" style={{ color: "var(--gold-deep)", fontSize: "0.55rem" }}>View</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Modal open={!!active} onClose={() => setActive(null)} title="Booking Details" testid="booking-modal">
        {active && (
          <div className="space-y-5">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 font-sans-j text-sm">
              <Info k="Reference" v={active.reference} />
              <Info k="Status" v={<StatusBadge status={active.status} />} />
              <Info k="Customer" v={active.customer_name} />
              <Info k="Phone" v={active.customer_phone} />
              <Info k="Email" v={active.customer_email} />
              <Info k="Duration" v={`${active.duration} min`} />
              <Info k="Boutique" v={active.shop_name} />
              <Info k="Appointment" v={active.appointment_type_name} />
            </div>
            {active.notes && <div><span className="field-label block mb-1">Customer Notes</span><p className="font-sans-j text-sm">{active.notes}</p></div>}
            {active.answers && active.answers.length > 0 && (
              <div data-testid="booking-answers"><span className="field-label block mb-2">Responses</span>
                <div className="space-y-1">{active.answers.map((a, i) => (
                  <div key={i} className="font-sans-j text-sm"><b>{a.label}:</b> {a.value}</div>
                ))}</div>
              </div>
            )}

            <div className="border-t pt-5" style={{ borderColor: "var(--line)" }}>
              <p className="field-label mb-3">Reschedule / Edit</p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
                <Field label="Date"><input type="date" className="input-wtb" value={resched.date} data-testid="resched-date"
                  onChange={(e) => setResched({ ...resched, date: e.target.value })} /></Field>
                <Field label="Time"><input type="time" className="input-wtb" value={resched.start_time} data-testid="resched-time"
                  onChange={(e) => setResched({ ...resched, start_time: e.target.value })} /></Field>
              </div>
              <Field label="Admin Notes"><textarea className="input-wtb" rows={2} value={resched.admin_notes} data-testid="resched-notes"
                onChange={(e) => setResched({ ...resched, admin_notes: e.target.value })} /></Field>
              <button className="btn-wtb btn-ghost-wtb mt-4 w-full" onClick={saveResched} data-testid="save-resched">Save Changes</button>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 border-t pt-5" style={{ borderColor: "var(--line)" }}>
              <button className="btn-wtb btn-gold" onClick={() => setStatus(active, "confirmed")} data-testid="confirm-booking">Confirm</button>
              <button className="btn-wtb btn-ghost-wtb" onClick={() => setStatus(active, "completed")} data-testid="complete-booking">Complete</button>
              <button className="btn-wtb" style={{ borderColor: "#9a4a3f", color: "#9a4a3f" }} onClick={() => setStatus(active, "cancelled")} data-testid="cancel-booking">Cancel</button>
            </div>
            <button className="btn-wtb btn-ghost-wtb w-full" onClick={() => setActive(null)} data-testid="close-booking-modal">Close</button>
          </div>
        )}
      </Modal>
    </div>
  );
}

function Info({ k, v }) {
  return <div><span className="field-label block mb-1">{k}</span><span style={{ color: "var(--charcoal)" }}>{v}</span></div>;
}
