# Wife To Be — Appointments App (PRD)

## Original Problem Statement
Bridal boutique group (wifetobe.co.uk, hosted on Hostinger) wants a self-hosted (TrueNAS SCALE "Goldeye", nginx, port 30039) appointment booking app at subdomain appointments.wifetobe.co.uk. "Book an Appointment" buttons on the site forward here. Must be robust, elegant, and match the existing site. Admin panel where the first admin (superadmin) creates up to 4 other admins; all admins manage calendars/availability for both shops and do usual appointment admin. Owner = superadmin.

## Architecture
- Backend: FastAPI (single server.py), MongoDB (motor). JWT Bearer auth (localStorage token 'wtb_token'), bcrypt, optional TOTP 2FA (pyotp + qrcode).
- Frontend: React (CRA/craco), react-router, Tailwind + custom brand CSS, sonner toasts, lucide icons.
- Brand tokens taken exactly from wifetobe.co.uk/assets/styles.css: fonts Cormorant Garamond / Jost / Pinyon Script; palette ivory/blush/champagne/gold/charcoal.

## User Personas
- Bride/Customer: books an appointment (no login).
- Admin (up to 4): manage bookings, availability, appointment types, own account/2FA/password.
- Superadmin (owner): all admin powers + manage admins + business email/notification settings.

## Core Requirements (static)
- Two shops: Warrington (wedding dresses) & Runcorn (suit hire).
- Appointment durations: 30/60/90/120.
- Per-shop weekly opening hours, slot intervals, blocked dates.
- Booking statuses: pending/confirmed/completed/cancelled.
- Email/password login, self password change, optional Google Authenticator 2FA (default off).
- Superadmin sets one generic business email + SMTP + notification toggles.

## Implemented (2026-07-08)
- Public multi-step booking flow (shop → type → date calendar → time slot → details → confirmation w/ WTB- reference).
- Slot availability engine (opening hours, blocked dates, overlap prevention).
- Admin auth + JWT + optional TOTP 2FA lifecycle (setup/enable/verify/disable).
- Admin dashboard, bookings management (confirm/complete/cancel/reschedule/filter), availability editor, appointment types CRUD, admins management (max 4), settings (business email/SMTP + notification toggles).
- Seed: superadmin, 2 shops, default hours, sample appointment types. DB indexes.
- Email framework built (SMTP send helper + toggles); notifications default OFF, wire specifics later.
- Tested: backend 30/30 pytest pass, frontend e2e pass (iteration_1.json).

## Enhancements (2026-06) — all additive, backward-compatible
- Per-shop custom booking questions (text/textarea/date/dropdown, required) via Customise page; answers stored on booking + shown in admin detail.
- Slot capacity (concurrent rooms) + buffer minutes between appointments; intra-day/partial-day blocked time ranges.
- CSV export of bookings; read-only iCal subscription feed ({BACKEND}/api/calendar/{token}.ics) for Google/Apple Calendar.
- Add-to-calendar (Google link + .ics) on the confirmation screen.
- Boutique info (photo + opening-hours text + what-to-expect) editable per shop, shown on public cards.
- Customer waitlist when no slots available; admin Waitlist page (mark contacted / delete).
- Per-admin SMTP settings + self-service profile/email; fully mobile-responsive; portalled, floating, easily-closable modals.
- Verified: iteration_8 (7 features), frontend 100%.

## Backlog / Remaining
- P1: Wire/verify automatic emails once SMTP configured; add reminder emails (24h before).
- P2: Public booking rate limiting/captcha; reschedule slot re-validation in UI; customer self-manage booking via reference link.
- P2: Split server.py into routers; migrate to FastAPI lifespan; per-admin activity log.

## Deployment notes (for user, self-host)
- Point DNS appointments.wifetobe.co.uk → TrueNAS external IP; nginx reverse proxy → app (frontend serves, backend on /api), app port 30039. Use Let's Encrypt SSL.
- Change site "Book an Appointment" buttons (Hostinger HTML) to href="https://appointments.wifetobe.co.uk".
- Set real SUPERADMIN_EMAIL/PASSWORD, JWT_SECRET, MONGO_URL, DB_NAME in backend/.env on the server; REACT_APP_BACKEND_URL in frontend/.env to the subdomain.

## Test Credentials
- Superadmin: superadmin@wifetobe.co.uk / WifeToBe2026!
