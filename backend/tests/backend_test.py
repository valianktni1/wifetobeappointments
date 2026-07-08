"""End-to-end backend API tests for Wife To Be appointment booking app."""
import os
import time
from datetime import date, timedelta

import pyotp
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://shop-scheduler-17.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

SUPERADMIN_EMAIL = "superadmin@wifetobe.co.uk"
SUPERADMIN_PASSWORD = "WifeToBe2026!"


# ---------- fixtures ----------
@pytest.fixture(scope="session")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def super_token(session):
    r = session.post(f"{API}/auth/login", json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "access_token" in data, data
    assert data["user"]["role"] == "superadmin"
    return data["access_token"]


@pytest.fixture(scope="session")
def super_headers(super_token):
    return {"Authorization": f"Bearer {super_token}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def shops(session):
    r = session.get(f"{API}/shops")
    assert r.status_code == 200
    return r.json()


@pytest.fixture(scope="session")
def warrington(shops):
    return next(s for s in shops if "warrington" in s["slug"].lower())


@pytest.fixture(scope="session")
def runcorn(shops):
    return next(s for s in shops if "runcorn" in s["slug"].lower())


def next_weekday(target_weekday: int, base: date = None) -> date:
    """target_weekday: Mon=0..Sun=6"""
    if base is None:
        base = date.today()
    days = (target_weekday - base.weekday()) % 7
    if days == 0:
        days = 7
    return base + timedelta(days=days)


# ---------- Shops (public) ----------
class TestShops:
    def test_list_shops(self, shops):
        assert len(shops) >= 2
        names = [s["name"] for s in shops]
        assert any("Warrington" in n for n in names)
        assert any("Runcorn" in n for n in names)

    def test_warrington_details(self, warrington):
        assert "3-5 Fennel Street" in warrington["address"]
        assert "WA1 2PA" in warrington["address"]

    def test_runcorn_details(self, runcorn):
        assert "136 Greenway Road" in runcorn["address"]
        assert "WA7 5BS" in runcorn["address"]


# ---------- Auth ----------
class TestAuth:
    def test_login_success(self, super_token):
        assert super_token

    def test_login_bad_password(self, session):
        r = session.post(f"{API}/auth/login", json={"email": SUPERADMIN_EMAIL, "password": "wrong!"})
        assert r.status_code == 401

    def test_me(self, session, super_headers):
        r = session.get(f"{API}/auth/me", headers=super_headers)
        assert r.status_code == 200
        assert r.json()["email"] == SUPERADMIN_EMAIL

    def test_me_unauth(self, session):
        r = session.get(f"{API}/auth/me")
        assert r.status_code == 401


# ---------- Public slots + booking ----------
class TestPublicBooking:
    def test_slots_on_monday_closed(self, session, warrington):
        d = next_weekday(0)  # Monday
        r = session.get(f"{API}/public/slots", params={"shop_id": warrington["id"], "date": d.isoformat(), "duration": 60})
        assert r.status_code == 200
        assert r.json()["slots"] == []

    def test_slots_on_tuesday(self, session, warrington):
        d = next_weekday(1)  # Tuesday
        r = session.get(f"{API}/public/slots", params={"shop_id": warrington["id"], "date": d.isoformat(), "duration": 60})
        assert r.status_code == 200
        slots = r.json()["slots"]
        assert len(slots) > 0
        assert "11:00" in slots

    def test_appointment_types_public(self, session, warrington):
        r = session.get(f"{API}/shops/{warrington['id']}/appointment-types")
        assert r.status_code == 200
        types = r.json()
        assert len(types) >= 1
        assert all(t.get("active", True) for t in types)

    def test_full_booking_flow_and_overlap(self, session, warrington):
        # get types
        types = session.get(f"{API}/shops/{warrington['id']}/appointment-types").json()
        t60 = next(t for t in types if t["duration"] == 60)
        d = next_weekday(1)  # Tuesday
        params = {"shop_id": warrington["id"], "date": d.isoformat(), "duration": 60}
        slots = session.get(f"{API}/public/slots", params=params).json()["slots"]
        assert slots, "No slots available"
        picked = slots[0]

        payload = {
            "shop_id": warrington["id"],
            "appointment_type_id": t60["id"],
            "date": d.isoformat(),
            "start_time": picked,
            "customer_name": "TEST_Customer",
            "customer_email": "test_customer@example.com",
            "customer_phone": "01234567890",
            "notes": "TEST",
        }
        r = session.post(f"{API}/public/bookings", json=payload)
        assert r.status_code == 200, r.text
        booking = r.json()
        assert booking["reference"].startswith("WTB-")
        assert booking["status"] == "pending"

        # Re-fetch slots - picked slot should no longer be present
        slots2 = session.get(f"{API}/public/slots", params=params).json()["slots"]
        assert picked not in slots2, f"Overlap not blocked. picked={picked}, slots={slots2}"

        # Retrieve by reference
        r = session.get(f"{API}/public/bookings/{booking['reference']}")
        assert r.status_code == 200
        assert r.json()["reference"] == booking["reference"]

        # store for cleanup
        pytest.booking_id_created = booking["id"]

    def test_booking_bad_slot_rejected(self, session, warrington):
        types = session.get(f"{API}/shops/{warrington['id']}/appointment-types").json()
        t = types[0]
        d = next_weekday(0)  # closed Monday
        r = session.post(f"{API}/public/bookings", json={
            "shop_id": warrington["id"],
            "appointment_type_id": t["id"],
            "date": d.isoformat(),
            "start_time": "11:00",
            "customer_name": "TEST_x", "customer_email": "x@example.com",
            "customer_phone": "1", "notes": "",
        })
        assert r.status_code == 409


# ---------- Admins management ----------
class TestAdmins:
    created_ids = []

    def test_non_superadmin_forbidden(self, session):
        # create a temp admin then use its token to hit /admins
        pass  # covered indirectly; skipping to avoid extra state

    def test_list_admins(self, session, super_headers):
        r = session.get(f"{API}/admins", headers=super_headers)
        assert r.status_code == 200
        assert any(u["role"] == "superadmin" for u in r.json())

    def test_create_and_duplicate_and_max(self, session, super_headers):
        # cleanup any prior TEST admins first
        existing = session.get(f"{API}/admins", headers=super_headers).json()
        for u in existing:
            if u["role"] == "admin" and u["email"].startswith("test_admin"):
                session.delete(f"{API}/admins/{u['id']}", headers=super_headers)

        # Create 4 admins successfully
        for i in range(4):
            r = session.post(f"{API}/admins", headers=super_headers, json={
                "name": f"TEST Admin {i}", "email": f"test_admin{i}@example.com", "password": "Passw0rd!"
            })
            assert r.status_code == 200, r.text
            TestAdmins.created_ids.append(r.json()["id"])

        # 5th should fail
        r = session.post(f"{API}/admins", headers=super_headers, json={
            "name": "TEST 5", "email": "test_admin5@example.com", "password": "Passw0rd!"
        })
        assert r.status_code == 400
        assert "Maximum" in r.json()["detail"]

        # Duplicate: delete one, then try duplicate of another still-existing
        r = session.post(f"{API}/admins", headers=super_headers, json={
            "name": "Dup", "email": "test_admin0@example.com", "password": "Passw0rd!"
        })
        assert r.status_code == 400

    def test_patch_deactivate_and_delete(self, session, super_headers):
        aid = TestAdmins.created_ids[0]
        r = session.patch(f"{API}/admins/{aid}", headers=super_headers, json={"active": False})
        assert r.status_code == 200
        assert r.json()["active"] is False

        # login as deactivated should fail with 403
        r = session.post(f"{API}/auth/login", json={"email": "test_admin0@example.com", "password": "Passw0rd!"})
        assert r.status_code == 403

        # delete
        r = session.delete(f"{API}/admins/{aid}", headers=super_headers)
        assert r.status_code == 200
        TestAdmins.created_ids.pop(0)

    def test_cannot_delete_superadmin(self, session, super_headers):
        users = session.get(f"{API}/admins", headers=super_headers).json()
        sa = next(u for u in users if u["role"] == "superadmin")
        r = session.delete(f"{API}/admins/{sa['id']}", headers=super_headers)
        assert r.status_code == 400

    def test_non_superadmin_403(self, session, super_headers):
        # login as remaining admin
        r = session.post(f"{API}/auth/login", json={"email": "test_admin1@example.com", "password": "Passw0rd!"})
        assert r.status_code == 200
        tok = r.json()["access_token"]
        r = session.get(f"{API}/admins", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

    def test_cleanup_created_admins(self, session, super_headers):
        for aid in TestAdmins.created_ids:
            session.delete(f"{API}/admins/{aid}", headers=super_headers)
        TestAdmins.created_ids.clear()


# ---------- Change password ----------
class TestChangePassword:
    def test_change_password_flow(self, session, super_headers):
        # create a temp admin to test on
        r = session.post(f"{API}/admins", headers=super_headers, json={
            "name": "TEST PW", "email": "test_pw@example.com", "password": "OldPass1!"
        })
        assert r.status_code == 200
        aid = r.json()["id"]

        # login
        r = session.post(f"{API}/auth/login", json={"email": "test_pw@example.com", "password": "OldPass1!"})
        assert r.status_code == 200
        tok = r.json()["access_token"]
        h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

        # wrong current
        r = session.post(f"{API}/auth/change-password", headers=h, json={"current_password": "bad", "new_password": "NewPass1!"})
        assert r.status_code == 400

        # correct
        r = session.post(f"{API}/auth/change-password", headers=h, json={"current_password": "OldPass1!", "new_password": "NewPass1!"})
        assert r.status_code == 200

        # login with new pw
        r = session.post(f"{API}/auth/login", json={"email": "test_pw@example.com", "password": "NewPass1!"})
        assert r.status_code == 200

        session.delete(f"{API}/admins/{aid}", headers=super_headers)


# ---------- 2FA ----------
class TestTwoFA:
    def test_2fa_lifecycle(self, session, super_headers):
        # create temp admin to avoid disrupting superadmin
        r = session.post(f"{API}/admins", headers=super_headers, json={
            "name": "TEST 2FA", "email": "test_2fa@example.com", "password": "OldPass1!"
        })
        assert r.status_code == 200
        aid = r.json()["id"]

        r = session.post(f"{API}/auth/login", json={"email": "test_2fa@example.com", "password": "OldPass1!"})
        tok = r.json()["access_token"]
        h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

        r = session.post(f"{API}/auth/2fa/setup", headers=h)
        assert r.status_code == 200
        secret = r.json()["secret"]
        assert r.json()["qr"].startswith("data:image/png;base64,")

        code = pyotp.TOTP(secret).now()
        r = session.post(f"{API}/auth/2fa/enable", headers=h, json={"code": code})
        assert r.status_code == 200

        # login now requires mfa
        r = session.post(f"{API}/auth/login", json={"email": "test_2fa@example.com", "password": "OldPass1!"})
        assert r.status_code == 200
        data = r.json()
        assert data.get("mfa_required") is True
        mfa_token = data["mfa_token"]

        time.sleep(1)
        code2 = pyotp.TOTP(secret).now()
        r = session.post(f"{API}/auth/2fa/verify", json={"mfa_token": mfa_token, "code": code2})
        assert r.status_code == 200, r.text
        tok2 = r.json()["access_token"]

        r = session.post(f"{API}/auth/2fa/disable", headers={"Authorization": f"Bearer {tok2}"})
        assert r.status_code == 200

        session.delete(f"{API}/admins/{aid}", headers=super_headers)


# ---------- Appointment types (admin) ----------
class TestAppointmentTypes:
    def test_create_bad_duration(self, session, super_headers, warrington):
        r = session.post(f"{API}/shops/{warrington['id']}/appointment-types", headers=super_headers,
                         json={"name": "TEST bad", "duration": 45, "description": "", "active": True})
        assert r.status_code == 400

    def test_crud(self, session, super_headers, warrington):
        r = session.post(f"{API}/shops/{warrington['id']}/appointment-types", headers=super_headers,
                         json={"name": "TEST T", "duration": 30, "description": "d", "active": True})
        assert r.status_code == 200
        tid = r.json()["id"]

        r = session.patch(f"{API}/appointment-types/{tid}", headers=super_headers,
                          json={"name": "TEST T2", "duration": 60, "description": "d2", "active": False})
        assert r.status_code == 200
        assert r.json()["name"] == "TEST T2"

        # all=true should show inactive
        r = session.get(f"{API}/shops/{warrington['id']}/appointment-types", params={"all": "true"})
        assert any(t["id"] == tid for t in r.json())

        # active only should not
        r = session.get(f"{API}/shops/{warrington['id']}/appointment-types")
        assert not any(t["id"] == tid for t in r.json())

        r = session.delete(f"{API}/appointment-types/{tid}", headers=super_headers)
        assert r.status_code == 200


# ---------- Availability + blocked dates ----------
class TestAvailability:
    def test_get_availability(self, session, warrington):
        r = session.get(f"{API}/shops/{warrington['id']}/availability")
        assert r.status_code == 200
        assert "hours" in r.json()

    def test_blocked_date(self, session, super_headers, warrington):
        d = next_weekday(1) + timedelta(days=7)  # future Tuesday
        r = session.post(f"{API}/shops/{warrington['id']}/blocked-dates", headers=super_headers,
                         json={"date": d.isoformat(), "reason": "TEST"})
        assert r.status_code == 200
        bid = r.json()["id"]

        r = session.get(f"{API}/public/slots", params={"shop_id": warrington["id"], "date": d.isoformat(), "duration": 60})
        assert r.status_code == 200
        assert r.json()["slots"] == []

        r = session.delete(f"{API}/blocked-dates/{bid}", headers=super_headers)
        assert r.status_code == 200


# ---------- Bookings admin ----------
class TestBookingsAdmin:
    def test_list_and_update(self, session, super_headers, warrington):
        r = session.get(f"{API}/bookings", headers=super_headers, params={"shop_id": warrington["id"]})
        assert r.status_code == 200
        bookings = r.json()
        if not bookings:
            pytest.skip("No bookings to test update on")
        bid = bookings[0]["id"]
        r = session.patch(f"{API}/bookings/{bid}", headers=super_headers,
                          json={"status": "confirmed", "admin_notes": "TEST confirmed"})
        assert r.status_code == 200
        assert r.json()["status"] == "confirmed"

        # bad status
        r = session.patch(f"{API}/bookings/{bid}", headers=super_headers, json={"status": "weird"})
        assert r.status_code == 400


# ---------- Dashboard ----------
class TestDashboard:
    def test_stats(self, session, super_headers):
        r = session.get(f"{API}/dashboard/stats", headers=super_headers)
        assert r.status_code == 200
        d = r.json()
        for k in ("total", "pending", "confirmed", "upcoming", "per_shop"):
            assert k in d
        assert len(d["per_shop"]) >= 2


# ---------- Settings ----------
class TestSettings:
    def test_get_settings_masked(self, session, super_headers):
        r = session.get(f"{API}/settings", headers=super_headers)
        assert r.status_code == 200
        d = r.json()
        assert d["smtp_password"] in ("", "********")

    def test_put_settings(self, session, super_headers):
        r = session.put(f"{API}/settings", headers=super_headers,
                        json={"business_email": "biz@example.com", "notify_customer_on_booking": False})
        assert r.status_code == 200
        r = session.get(f"{API}/settings", headers=super_headers)
        assert r.json()["business_email"] == "biz@example.com"

    def test_settings_forbidden_for_admin(self, session, super_headers):
        # create temp admin
        r = session.post(f"{API}/admins", headers=super_headers, json={
            "name": "TEST S", "email": "test_settings@example.com", "password": "Passw0rd!"
        })
        assert r.status_code == 200
        aid = r.json()["id"]
        r = session.post(f"{API}/auth/login", json={"email": "test_settings@example.com", "password": "Passw0rd!"})
        tok = r.json()["access_token"]
        r = session.get(f"{API}/settings", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403
        session.delete(f"{API}/admins/{aid}", headers=super_headers)


# ---------- final cleanup: remove TEST bookings ----------
@pytest.fixture(scope="session", autouse=True)
def cleanup_bookings(super_headers):
    yield
    try:
        r = requests.get(f"{API}/bookings", headers=super_headers)
        for b in r.json():
            if b.get("customer_email", "").startswith("test_customer") or b.get("customer_name", "").startswith("TEST"):
                requests.patch(f"{API}/bookings/{b['id']}", headers=super_headers, json={"status": "cancelled"})
    except Exception:
        pass
