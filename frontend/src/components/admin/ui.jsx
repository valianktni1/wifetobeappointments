import React from "react";
import { X } from "lucide-react";
import { Eyebrow, GoldRule } from "@/components/Brand";

export function PageHead({ eyebrow, title, children }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 mb-8">
      <div>
        <Eyebrow>{eyebrow}</Eyebrow>
        <h1 className="text-4xl md:text-5xl mt-2">{title}</h1>
        <GoldRule className="mt-4" />
      </div>
      <div className="flex items-center gap-3">{children}</div>
    </div>
  );
}

export function Panel({ children, className = "" }) {
  return <div className={`card-wtb p-6 md:p-8 ${className}`}>{children}</div>;
}

export function Field({ label, children }) {
  return (
    <div>
      <label className="field-label block mb-2">{label}</label>
      {children}
    </div>
  );
}

export function Toggle({ checked, onChange, testid }) {
  return (
    <button type="button" onClick={() => onChange(!checked)} data-testid={testid}
      className="relative inline-flex h-6 w-11 items-center rounded-full transition-colors"
      style={{ background: checked ? "var(--gold)" : "var(--line)" }}>
      <span className="inline-block h-4 w-4 transform rounded-full bg-white transition-transform"
        style={{ transform: checked ? "translateX(22px)" : "translateX(4px)" }} />
    </button>
  );
}

export function Modal({ open, onClose, title, children, testid }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" data-testid={testid}>
      <div className="absolute inset-0" style={{ background: "rgba(42,37,33,.5)" }} onClick={onClose} />
      <div className="relative card-wtb p-8 w-full max-w-lg reveal-up max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-6">
          <h3 className="text-3xl">{title}</h3>
          <button onClick={onClose} className="text-taupe hover:text-[var(--charcoal)]" data-testid="modal-close" style={{ color: "var(--taupe)" }}><X size={20} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function StatusBadge({ status }) {
  const map = {
    pending: { bg: "var(--champagne)", c: "var(--gold-deep)" },
    confirmed: { bg: "#DCEAD9", c: "#3f6b39" },
    cancelled: { bg: "#F0DAD6", c: "#9a4a3f" },
    completed: { bg: "var(--ivory-2)", c: "var(--taupe)" },
  };
  const s = map[status] || map.pending;
  return (
    <span className="eyebrow px-3 py-1 inline-block" style={{ background: s.bg, color: s.c, fontSize: "0.55rem" }} data-testid={`status-${status}`}>
      {status}
    </span>
  );
}
