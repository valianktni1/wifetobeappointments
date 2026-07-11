import React, { useState } from "react";
import { PayPalScriptProvider, PayPalButtons } from "@paypal/react-paypal-js";
import { toast } from "sonner";
import api, { apiErr } from "@/lib/api";

const CUR = { GBP: "£", USD: "$", EUR: "€" };

function money(amount, currency) {
  const sym = CUR[currency] || "";
  return `${sym}${Number(amount || 0).toFixed(2)}`;
}

export default function PaymentPanel({ booking, config, onUpdate }) {
  const [busy, setBusy] = useState(false);
  const [claimed, setClaimed] = useState(false);
  if (!booking || !config) return null;

  const amount = Number(booking.deposit_amount || 0);
  const status = booking.payment_status || "not_required";
  const cur = config.currency || "GBP";

  if (status === "not_required" || amount <= 0) return null;

  if (status === "paid") {
    return (
      <div className="border-t border-b py-5 my-6 text-center" style={{ borderColor: "var(--line)" }} data-testid="payment-paid">
        <p className="font-sans-j text-sm" style={{ color: "#3f6b39" }}>
          ✓ Deposit of {money(amount, cur)} paid — thank you.
        </p>
      </div>
    );
  }

  if (status === "pay_in_person") {
    return (
      <div className="border-t border-b py-5 my-6 text-center" style={{ borderColor: "var(--line)" }} data-testid="payment-in-person">
        <p className="font-sans-j text-sm" style={{ color: "var(--taupe)" }}>
          A deposit of {money(amount, cur)} is payable in person / on the day of your appointment.
        </p>
      </div>
    );
  }

  // status === "pending"
  const method = config.method;
  const required = !!booking.deposit_required;

  const payInPerson = async () => {
    setBusy(true);
    try {
      const { data } = await api.post(`/public/bookings/${booking.reference}/pay-in-person`);
      onUpdate?.(data);
      toast.success("Noted — please pay on the day.");
    } catch (e) { toast.error(apiErr(e)); }
    finally { setBusy(false); }
  };

  const notifyPaid = async () => {
    setBusy(true);
    try {
      const { data } = await api.post(`/public/bookings/${booking.reference}/notify-paid`);
      onUpdate?.(data);
      setClaimed(true);
      toast.success("Thank you — we'll confirm your deposit shortly.");
    } catch (e) { toast.error(apiErr(e)); }
    finally { setBusy(false); }
  };

  const hasClaimed = claimed || booking.deposit_claimed;

  return (
    <div className="border-t border-b py-6 my-6 text-center" style={{ borderColor: "var(--line)" }} data-testid="payment-panel">
      <p className="field-label mb-1">Deposit Due</p>
      <p className="font-serif-c text-3xl mb-4" style={{ color: "var(--gold-deep)" }} data-testid="payment-amount">{money(amount, cur)}</p>

      {method === "in_person" && (
        <p className="font-sans-j text-sm" style={{ color: "var(--taupe)" }}>Please pay in person / on the day of your appointment.</p>
      )}

      {method === "paypal_me" && config.paypal_me_url && (
        <div className="space-y-3">
          <a className="btn-wtb btn-gold inline-block" data-testid="paypalme-link" target="_blank" rel="noreferrer"
            href={`${config.paypal_me_url}/${amount.toFixed(2)}${cur}`}>Pay {money(amount, cur)} with PayPal</a>
          <p className="font-sans-j text-xs" style={{ color: "var(--taupe)" }}>
            Please use your reference <b>{booking.reference}</b> as the payment note.
          </p>
          {hasClaimed ? (
            <p className="font-sans-j text-sm" data-testid="deposit-claimed" style={{ color: "#3f6b39" }}>
              Thank you — we'll confirm your deposit shortly.
            </p>
          ) : (
            <button className="btn-wtb btn-ghost-wtb" onClick={notifyPaid} disabled={busy} data-testid="notify-paid-btn">
              I've paid — notify the boutique
            </button>
          )}
        </div>
      )}

      {method === "paypal" && config.paypal_configured && (
        <div className="max-w-xs mx-auto" data-testid="paypal-buttons">
          <PayPalScriptProvider options={{ clientId: config.paypal_client_id, currency: cur }}>
            <PayPalButtons
              style={{ layout: "vertical", color: "gold", shape: "rect" }}
              createOrder={async () => {
                const { data } = await api.post(`/public/bookings/${booking.reference}/paypal/create-order`);
                return data.id;
              }}
              onApprove={async (data) => {
                try {
                  const { data: b } = await api.post(`/public/bookings/${booking.reference}/paypal/capture-order`, null, { params: { order_id: data.orderID } });
                  onUpdate?.(b);
                  toast.success("Payment received — thank you!");
                } catch (e) { toast.error(apiErr(e)); }
              }}
              onError={() => toast.error("PayPal payment could not be completed.")}
            />
          </PayPalScriptProvider>
        </div>
      )}

      {method === "paypal" && !config.paypal_configured && (
        <p className="font-sans-j text-sm" style={{ color: "var(--taupe)" }}>Online payment is not yet available — we'll take your deposit another way.</p>
      )}

      {!required && (method === "paypal_me" || method === "paypal") && (
        <button className="block mx-auto mt-5 eyebrow hover:opacity-70" style={{ fontSize: "0.6rem", color: "var(--taupe)" }}
          onClick={payInPerson} disabled={busy} data-testid="pay-in-person-btn">
          I'll pay in person instead →
        </button>
      )}
    </div>
  );
}
