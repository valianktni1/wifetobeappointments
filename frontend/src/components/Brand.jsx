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
