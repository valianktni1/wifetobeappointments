"""Tests for SMTP encryption fix — verifies SSL/TLS/None handling in _smtp_send."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://shop-scheduler-17.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

SUPER_EMAIL = "superadmin@wifetobe.co.uk"
SUPER_PASS = "WifeToBe2026!"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{API}/auth/login", json={"email": SUPER_EMAIL, "password": SUPER_PASS}, timeout=15)
    assert r.status_code == 200, f"Superadmin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "access_token" in data
    return data["access_token"]


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def original_settings(auth_headers):
    r = requests.get(f"{API}/settings", headers=auth_headers, timeout=15)
    assert r.status_code == 200
    return r.json()


def _save_settings(headers, **fields):
    r = requests.put(f"{API}/settings", headers=headers, json=fields, timeout=15)
    assert r.status_code in (200, 204), f"Save settings failed: {r.status_code} {r.text}"
    return r


def _test_email(headers, to="alice@example.com"):
    return requests.post(f"{API}/settings/test-email", headers=headers, json={"to": to}, timeout=45)


# ---- Settings persistence ----

def test_settings_persists_smtp_encryption_and_masks_password(auth_headers):
    _save_settings(auth_headers,
                   business_email="alice@example.com",
                   from_name="Wife To Be Test",
                   smtp_host="smtp.gmail.com",
                   smtp_port=465,
                   smtp_encryption="ssl",
                   smtp_user="alice@gmail.com",
                   smtp_password="wrongpass123")
    r = requests.get(f"{API}/settings", headers=auth_headers, timeout=15)
    assert r.status_code == 200
    s = r.json()
    assert s.get("smtp_encryption") == "ssl"
    assert s.get("smtp_port") == 465
    # Password must be masked
    pw = s.get("smtp_password")
    assert pw in ("********", None, "") or pw == "********", f"Password not masked: {pw!r}"


# ---- SSL 465 path ----

def test_ssl_465_path_reaches_auth_stage(auth_headers):
    _save_settings(auth_headers,
                   business_email="alice@example.com",
                   from_name="WTB",
                   smtp_host="smtp.gmail.com",
                   smtp_port=465,
                   smtp_encryption="ssl",
                   smtp_user="alice@gmail.com",
                   smtp_password="definitelyWrong!!")
    r = _test_email(auth_headers)
    assert r.status_code == 400, f"Expected 400 auth error, got {r.status_code}: {r.text}"
    detail = (r.json().get("detail") or "").lower()
    # Must be an AUTH-rejection message (proves TLS handshake succeeded)
    assert ("username" in detail or "password" in detail or "app password" in detail
            or "authentication" in detail or "rejected" in detail), \
        f"Expected auth-rejected message, got: {detail}"


# ---- TLS 587 path ----

def test_tls_587_path_reaches_auth_stage(auth_headers):
    _save_settings(auth_headers,
                   business_email="alice@example.com",
                   from_name="WTB",
                   smtp_host="smtp.gmail.com",
                   smtp_port=587,
                   smtp_encryption="tls",
                   smtp_user="alice@gmail.com",
                   smtp_password="definitelyWrong!!")
    r = _test_email(auth_headers)
    assert r.status_code == 400, f"Expected 400 auth error, got {r.status_code}: {r.text}"
    detail = (r.json().get("detail") or "").lower()
    assert ("username" in detail or "password" in detail or "app password" in detail
            or "authentication" in detail or "rejected" in detail), \
        f"Expected auth-rejected message, got: {detail}"


# ---- Bad host clear error ----

def test_bad_host_returns_400_with_clear_message(auth_headers):
    _save_settings(auth_headers,
                   business_email="alice@example.com",
                   from_name="WTB",
                   smtp_host="smtp.invalidhost.test",
                   smtp_port=587,
                   smtp_encryption="tls",
                   smtp_user="x@invalidhost.test",
                   smtp_password="whatever")
    r = _test_email(auth_headers)
    assert r.status_code == 400, f"Expected 400 (not 500), got {r.status_code}: {r.text}"
    detail = (r.json().get("detail") or "").lower()
    assert any(k in detail for k in ["connect", "name", "resolve", "host", "not known", "could not"]), \
        f"Expected descriptive connect error, got: {detail}"


# ---- Test-email requires config ----

def test_test_email_requires_config(auth_headers):
    # Wipe settings
    _save_settings(auth_headers,
                   business_email="",
                   smtp_host="",
                   smtp_user="",
                   smtp_password="",
                   smtp_encryption="tls",
                   smtp_port=587)
    r = _test_email(auth_headers)
    assert r.status_code == 400
    detail = r.json().get("detail", "").lower()
    assert "business" in detail or "smtp" in detail or "configured" in detail


# ---- Regression: public booking works without SMTP ----

def test_public_booking_still_works_without_smtp(auth_headers):
    # Ensure no SMTP configured (from previous test)
    r = requests.get(f"{API}/shops", timeout=15)
    if r.status_code != 200 or not r.json():
        pytest.skip("No shops available for booking regression test")
    shop = r.json()[0]
    shop_id = shop["id"]
    # Get an appointment type
    r2 = requests.get(f"{API}/shops/{shop_id}/appointment-types", timeout=15)
    if r2.status_code != 200 or not r2.json():
        pytest.skip("No appointment types available")
    apt = r2.json()[0]
    # Try a booking — the crucial part is: no crash if SMTP missing
    from datetime import datetime, timedelta
    date = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
    slots = requests.get(f"{API}/public/slots",
                         params={"shop_id": shop_id, "date": date, "duration": apt.get("duration", 60)},
                         timeout=15)
    if slots.status_code != 200:
        pytest.skip("Slots endpoint failed")
    slot_data = slots.json()
    slot_list = slot_data.get("slots", []) if isinstance(slot_data, dict) else slot_data
    if not slot_list:
        pytest.skip("Empty slot list")
    first = slot_list[0]
    time_slot = first if isinstance(first, str) else first.get("time") or first.get("start")
    if not time_slot:
        pytest.skip("Could not extract slot time")
    payload = {
        "shop_id": shop_id,
        "appointment_type_id": apt["id"],
        "date": date,
        "start_time": time_slot,
        "duration": apt.get("duration", 60),
        "customer_name": "TEST_Regression User",
        "customer_email": "TEST_regression@example.com",
        "customer_phone": "+441234567890",
    }
    br = requests.post(f"{API}/public/bookings", json=payload, timeout=20)
    # Should NOT be 500 even without SMTP
    assert br.status_code < 500, f"Public booking crashed without SMTP: {br.status_code} {br.text}"
    assert br.status_code in (200, 201, 400, 409, 422), f"Unexpected status: {br.status_code} {br.text}"


# ---- Cleanup: reset settings & test bookings ----

def test_zzz_cleanup_reset_settings(auth_headers):
    _save_settings(auth_headers,
                   business_email="",
                   smtp_host="",
                   smtp_user="",
                   smtp_password="",
                   smtp_encryption="tls",
                   smtp_port=587)
    r = requests.get(f"{API}/settings", headers=auth_headers, timeout=15)
    s = r.json()
    assert (s.get("business_email") or "") == ""
    assert (s.get("smtp_host") or "") == ""


def test_zzz_cleanup_test_bookings(auth_headers):
    r = requests.get(f"{API}/bookings", headers=auth_headers, timeout=15)
    if r.status_code != 200:
        return
    for b in r.json():
        email = (b.get("customer_email") or "").lower()
        if "example.com" in email:
            bid = b.get("id") or b.get("_id")
            if bid:
                requests.delete(f"{API}/bookings/{bid}", headers=auth_headers, timeout=10)
