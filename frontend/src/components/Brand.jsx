import React from "react";

export function Wordmark({ className = "", size = "text-3xl" }) {
  return <span className={`wordmark ${size} ${className}`} data-testid="brand-wordmark">Wife To Be</span>;
}

export function Eyebrow({ children, className = "", ...props }) {
  return <span className={`eyebrow ${className}`} {...props}>{children}</span>;
}

export function GoldRule({ className = "" }) {
  return <span className={`gold-rule block ${className}`} aria-hidden />;
}

export function DesignerCredit({ dark = false }) {
  return (
    <div className="w-full text-center py-4" data-testid="designer-credit"
      style={{ borderTop: `1px solid ${dark ? "rgba(255,255,255,.15)" : "var(--line)"}` }}>
      <span className="eyebrow" style={{ fontSize: "0.5rem", letterSpacing: "0.28em", color: dark ? "rgba(255,255,255,.8)" : "var(--taupe)" }}>
        Site Designed and Hosted By Weddings By Mark
      </span>
    </div>
  );
}
