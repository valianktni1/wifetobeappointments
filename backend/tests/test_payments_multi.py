"""Tests for the MULTI-METHOD payments framework (iteration 11).

Covers:
 - GET /api/payments/config shape (methods list, method legacy, bank block).
 - PUT /api/settings persists payment_methods (capped to 3), bank fields, paypal_me_url.
 - Server enforces 3-cap on payment_methods.
 - Booking payment_status matrix:
      no methods                       -> not_required
      only in_person                   -> pay_in_person
      >=1 online method                -> pending
 - POST /public/bookings/{ref}/notify-paid sets deposit_claimed True (status stays pending).
 - POST /public/bookings/{ref}/pay-in-person sets payment_status pay_in_person.
 - PayPal card create-order returns 400 while unconfigured.
 - Restores settings + shop deposits at end (methods=[], deposits=0).
"""
import os
import requests
import pytest
from datetime import date, timedelta


def _base():
    v = os.environ.get('REACT_APP_BACKEND_URL')
    if v:
        return v.rstrip('/')
    for line in open('/app/frontend/.env'):
        if line.startswith('REACT_APP_BACKEND_URL='):
            return line.split('=', 1)[1].strip().rstrip('/')
    raise RuntimeError("REACT_APP_BACKEND_URL not set")


BASE = _base()
API = f"{BASE}/api"
SUPERADMIN = {"email": "superadmin@wifetobe.co.uk", "password": "WifeToBe2026!"}


@pytest.fixture(scope="module")
def headers():
    r = requests.post(f"{API}/auth/login", json=SUPERADMIN, timeout=15)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def original_settings(headers):
    return requests.get(f"{API}/settings", headers=headers, timeout=15).json()


@pytest.fixture(scope="module")
def shops(headers):
    r = requests.get(f"{API}/shops", timeout=15)
    assert r.status_code == 200 and r.json()
    return r.json()


@pytest.fixture(scope="module")
def a_shop(shops):
    return shops[0]


@pytest.fixture(scope="module")
def original_shop_states(shops):
    return [(s["id"], s.get("deposit_amount", 0), s.get("deposit_required", False)) for s in shops]


@pytest.fixture(scope="module", autouse=True)
def _cleanup(headers, original_settings, original_shop_states):
    yield
    requests.put(f"{API}/settings", headers=headers, json={
        "payment_method": "off",
        "payment_methods": [],
        "paypal_me_url": "",
        "bank_account_name": "",
        "bank_sort_code": "",
        "bank_account_number": "",
        "payment_currency": original_settings.get("payment_currency", "GBP"),
    }, timeout=15)
    for sid, amt, req in original_shop_states:
        requests.patch(f"{API}/shops/{sid}", headers=headers,
                       json={"deposit_amount": 0, "deposit_required": False}, timeout=15)


def _find_slot(shop_id, atype):
    for i in range(1, 60):
        d = (date.today() + timedelta(days=i)).isoformat()
        r = requests.get(f"{API}/public/slots",
                         params={"shop_id": shop_id, "date": d, "duration": atype["duration"]}, timeout=15)
        if r.status_code == 200:
            slots = r.json().get("slots") or []
            if slots:
                return d, slots[0]
    return None, None


def _atype(shop_id):
    r = requests.get(f"{API}/shops/{shop_id}/appointment-types", timeout=15).json()
    return r[0]


def _mk(shop_id, atype, name="TEST_Multi", email="test_multi@example.com"):
    d, slot = _find_slot(shop_id, atype)
    assert slot, "No open slot in next 60 days"
    payload = {
        "shop_id": shop_id, "appointment_type_id": atype["id"],
        "date": d, "start_time": slot,
        "customer_name": name, "customer_email": email, "customer_phone": "07000000000",
        "notes": "", "answers": [],
    }
    r = requests.post(f"{API}/public/bookings", json=payload, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- config shape
class TestConfigShape:
    def test_shape(self):
        r = requests.get(f"{API}/payments/config", timeout=15)
        assert r.status_code == 200
        d = r.json()
        for k in ("methods", "method", "paypal_me_url", "bank", "currency",
                  "paypal_client_id", "paypal_configured"):
            assert k in d, f"missing {k}"
        assert isinstance(d["methods"], list)
        assert set(d["bank"].keys()) == {"account_name", "sort_code", "account_number"}
        assert isinstance(d["paypal_configured"], bool)


# ---------------------------------------------------------------- settings PUT
class TestSettingsMethods:
    def test_set_two_methods_and_bank(self, headers):
        r = requests.put(f"{API}/settings", headers=headers, json={
            "payment_methods": ["paypal_me", "bank_transfer"],
            "paypal_me_url": "https://paypal.me/testshop",
            "bank_account_name": "Wife To Be Ltd",
            "bank_sort_code": "12-34-56",
            "bank_account_number": "12345678",
        }, timeout=15)
        assert r.status_code == 200, r.text
        cfg = requests.get(f"{API}/payments/config", timeout=15).json()
        assert set(cfg["methods"]) == {"paypal_me", "bank_transfer"}
        assert cfg["paypal_me_url"] == "https://paypal.me/testshop"
        assert cfg["bank"]["account_name"] == "Wife To Be Ltd"
        assert cfg["bank"]["sort_code"] == "12-34-56"
        assert cfg["bank"]["account_number"] == "12345678"

    def test_server_caps_to_three(self, headers):
        r = requests.put(f"{API}/settings", headers=headers, json={
            "payment_methods": ["paypal_me", "bank_transfer", "in_person", "paypal"],
        }, timeout=15)
        assert r.status_code == 200, r.text
        cfg = requests.get(f"{API}/payments/config", timeout=15).json()
        assert len(cfg["methods"]) == 3

    def test_reset_to_empty(self, headers):
        r = requests.put(f"{API}/settings", headers=headers, json={
            "payment_methods": [],
        }, timeout=15)
        assert r.status_code == 200
        cfg = requests.get(f"{API}/payments/config", timeout=15).json()
        assert cfg["methods"] == []
        assert cfg["method"] == "off"


# ---------------------------------------------------------------- booking matrix
class TestBookingMatrix:
    def test_no_methods_regression(self, headers, a_shop):
        requests.put(f"{API}/settings", headers=headers, json={"payment_methods": []}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=headers,
                       json={"deposit_amount": 25.0, "deposit_required": True}, timeout=15)
        b = _mk(a_shop["id"], _atype(a_shop["id"]))
        assert b["payment_status"] == "not_required"
        assert float(b.get("deposit_amount", 0)) == 0

    def test_only_in_person(self, headers, a_shop):
        requests.put(f"{API}/settings", headers=headers, json={"payment_methods": ["in_person"]}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=headers,
                       json={"deposit_amount": 25.0, "deposit_required": False}, timeout=15)
        b = _mk(a_shop["id"], _atype(a_shop["id"]))
        assert b["payment_status"] == "pay_in_person"
        assert float(b["deposit_amount"]) == 25.0

    def test_online_and_in_person_pending(self, headers, a_shop):
        requests.put(f"{API}/settings", headers=headers,
                     json={"payment_methods": ["paypal_me", "in_person"],
                           "paypal_me_url": "https://paypal.me/testshop"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=headers,
                       json={"deposit_amount": 30.0, "deposit_required": True}, timeout=15)
        b = _mk(a_shop["id"], _atype(a_shop["id"]))
        assert b["payment_status"] == "pending"
        assert float(b["deposit_amount"]) == 30.0
        pytest.mm_ref = b["reference"]
        pytest.mm_bid = b["id"]

    def test_notify_paid_marks_claim(self, headers, a_shop):
        requests.put(f"{API}/settings", headers=headers,
                     json={"payment_methods": ["paypal_me", "bank_transfer"],
                           "paypal_me_url": "https://paypal.me/testshop",
                           "bank_account_name": "Wife To Be Ltd",
                           "bank_sort_code": "12-34-56",
                           "bank_account_number": "12345678"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=headers,
                       json={"deposit_amount": 20.0, "deposit_required": True}, timeout=15)
        b = _mk(a_shop["id"], _atype(a_shop["id"]), name="TEST_Notify", email="test_notify@example.com")
        assert b["payment_status"] == "pending"
        r = requests.post(f"{API}/public/bookings/{b['reference']}/notify-paid", timeout=15)
        assert r.status_code == 200, r.text
        got = requests.get(f"{API}/public/bookings/{b['reference']}").json()
        assert got.get("deposit_claimed") is True
        assert got["payment_status"] == "pending"  # unchanged

    def test_pay_in_person_endpoint(self, headers, a_shop):
        requests.put(f"{API}/settings", headers=headers,
                     json={"payment_methods": ["paypal_me", "in_person"],
                           "paypal_me_url": "https://paypal.me/testshop"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=headers,
                       json={"deposit_amount": 20.0, "deposit_required": False}, timeout=15)
        b = _mk(a_shop["id"], _atype(a_shop["id"]), name="TEST_PIP2", email="test_pip2@example.com")
        assert b["payment_status"] == "pending"
        r = requests.post(f"{API}/public/bookings/{b['reference']}/pay-in-person", timeout=15)
        assert r.status_code == 200, r.text
        got = requests.get(f"{API}/public/bookings/{b['reference']}").json()
        assert got["payment_status"] == "pay_in_person"

    def test_paypal_create_order_400(self, headers, a_shop):
        requests.put(f"{API}/settings", headers=headers,
                     json={"payment_methods": ["paypal"]}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=headers,
                       json={"deposit_amount": 10.0, "deposit_required": True}, timeout=15)
        b = _mk(a_shop["id"], _atype(a_shop["id"]), name="TEST_PPC", email="test_ppc@example.com")
        r = requests.post(f"{API}/public/bookings/{b['reference']}/paypal/create-order", timeout=15)
        assert r.status_code == 400
        assert "not configured" in r.text.lower()


# ---------------------------------------------------------------- Admin regression
class TestAdminRegression:
    def test_dashboard_stats_ok(self, headers):
        r = requests.get(f"{API}/dashboard/stats", headers=headers, timeout=15)
        assert r.status_code == 200
        for k in ("total", "pending", "confirmed"):
            assert k in r.json()

    def test_csv_export_has_payment(self, headers):
        r = requests.get(f"{API}/bookings/export.csv", headers=headers, timeout=20)
        assert r.status_code == 200
        h = r.text.splitlines()[0].lower()
        assert "deposit" in h and "payment" in h
