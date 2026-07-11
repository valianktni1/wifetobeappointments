"""Tests for the payments framework (deposits, PayPal.me, in-person, PayPal card errors)."""
import os
import requests
import pytest
from datetime import date, timedelta

def _load_base():
    v = os.environ.get('REACT_APP_BACKEND_URL')
    if v:
        return v.rstrip('/')
    try:
        for line in open('/app/frontend/.env'):
            if line.startswith('REACT_APP_BACKEND_URL='):
                return line.split('=', 1)[1].strip().rstrip('/')
    except Exception:
        pass
    raise RuntimeError("REACT_APP_BACKEND_URL not set")

BASE_URL = _load_base()
API = f"{BASE_URL}/api"

SUPERADMIN = {"email": "superadmin@wifetobe.co.uk", "password": "WifeToBe2026!"}


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/login", json=SUPERADMIN, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def original_settings(auth_headers):
    r = requests.get(f"{API}/settings", headers=auth_headers, timeout=15)
    assert r.status_code == 200
    return r.json()


@pytest.fixture(scope="module")
def a_shop(auth_headers):
    r = requests.get(f"{API}/shops", timeout=15)
    assert r.status_code == 200
    shops = r.json()
    assert len(shops) > 0
    return shops[0]


@pytest.fixture(scope="module")
def original_shop_state(a_shop):
    return {
        "id": a_shop["id"],
        "deposit_amount": a_shop.get("deposit_amount", 0),
        "deposit_required": a_shop.get("deposit_required", False),
    }


def _find_open_slot(shop_id: str, atype_id: str, duration: int):
    for i in range(1, 60):
        d = (date.today() + timedelta(days=i)).isoformat()
        r = requests.get(f"{API}/public/slots",
                         params={"shop_id": shop_id, "date": d, "duration": duration}, timeout=15)
        if r.status_code == 200:
            slots = r.json().get("slots") or []
            if slots:
                return d, slots[0]
    return None, None


# ---------- Cleanup on module teardown ----------
@pytest.fixture(scope="module", autouse=True)
def cleanup(auth_headers, original_settings, original_shop_state):
    yield
    # Restore settings payment_method to 'off' and paypal_me_url back
    requests.put(f"{API}/settings", headers=auth_headers, json={
        "payment_method": original_settings.get("payment_method", "off"),
        "paypal_me_url": original_settings.get("paypal_me_url", ""),
        "payment_currency": original_settings.get("payment_currency", "GBP"),
    }, timeout=15)
    # Restore shop
    requests.patch(f"{API}/shops/{original_shop_state['id']}", headers=auth_headers, json={
        "deposit_amount": original_shop_state["deposit_amount"],
        "deposit_required": original_shop_state["deposit_required"],
    }, timeout=15)


# ---------- Public config ----------
class TestPaymentsConfig:
    def test_config_public_shape(self):
        r = requests.get(f"{API}/payments/config", timeout=15)
        assert r.status_code == 200
        d = r.json()
        for k in ("method", "paypal_me_url", "currency", "paypal_client_id", "paypal_configured"):
            assert k in d, f"missing {k}"
        assert isinstance(d["paypal_configured"], bool)


# ---------- Settings admin flow ----------
class TestSettingsPaymentMethod:
    def test_set_paypal_me(self, auth_headers):
        r = requests.put(f"{API}/settings", headers=auth_headers,
                         json={"payment_method": "paypal_me", "paypal_me_url": "https://paypal.me/testshop"}, timeout=15)
        assert r.status_code == 200, r.text
        cfg = requests.get(f"{API}/payments/config", timeout=15).json()
        assert cfg["method"] == "paypal_me"
        assert cfg["paypal_me_url"] == "https://paypal.me/testshop"

    def test_set_in_person(self, auth_headers):
        r = requests.put(f"{API}/settings", headers=auth_headers,
                         json={"payment_method": "in_person"}, timeout=15)
        assert r.status_code == 200
        assert requests.get(f"{API}/payments/config").json()["method"] == "in_person"

    def test_set_off(self, auth_headers):
        r = requests.put(f"{API}/settings", headers=auth_headers,
                         json={"payment_method": "off"}, timeout=15)
        assert r.status_code == 200
        assert requests.get(f"{API}/payments/config").json()["method"] == "off"


# ---------- Per-shop deposit ----------
class TestShopDeposit:
    def test_set_and_persist_deposit(self, auth_headers, a_shop):
        r = requests.patch(f"{API}/shops/{a_shop['id']}", headers=auth_headers,
                           json={"deposit_amount": 25.0, "deposit_required": False}, timeout=15)
        assert r.status_code == 200, r.text
        shops = requests.get(f"{API}/shops").json()
        found = next(s for s in shops if s["id"] == a_shop["id"])
        assert float(found.get("deposit_amount", 0)) == 25.0
        assert found.get("deposit_required") is False


# ---------- Booking payment logic matrix ----------
class TestBookingPaymentLogic:
    def _make_booking(self, shop_id, atype):
        d, slot = _find_open_slot(shop_id, atype["id"], atype["duration"])
        assert slot, "No open slot found in next 60 days"
        payload = {
            "shop_id": shop_id, "appointment_type_id": atype["id"],
            "date": d, "start_time": slot,
            "customer_name": "TEST_PayCustomer",
            "customer_email": "test_pay@example.com",
            "customer_phone": "07000000000",
            "notes": "", "answers": [],
        }
        r = requests.post(f"{API}/public/bookings", json=payload, timeout=20)
        assert r.status_code == 200, r.text
        return r.json()

    def _atype(self, shop_id):
        r = requests.get(f"{API}/shops/{shop_id}/appointment-types", timeout=15)
        assert r.status_code == 200 and r.json(), "No appointment types"
        return r.json()[0]

    def test_method_off_gives_not_required(self, auth_headers, a_shop):
        requests.put(f"{API}/settings", headers=auth_headers, json={"payment_method": "off"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=auth_headers,
                       json={"deposit_amount": 25.0, "deposit_required": True}, timeout=15)
        atype = self._atype(a_shop["id"])
        b = self._make_booking(a_shop["id"], atype)
        assert b["payment_status"] == "not_required"
        assert float(b.get("deposit_amount", 0)) == 0

    def test_in_person_gives_pay_in_person(self, auth_headers, a_shop):
        requests.put(f"{API}/settings", headers=auth_headers, json={"payment_method": "in_person"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=auth_headers,
                       json={"deposit_amount": 25.0, "deposit_required": False}, timeout=15)
        atype = self._atype(a_shop["id"])
        b = self._make_booking(a_shop["id"], atype)
        assert b["payment_status"] == "pay_in_person"
        assert float(b["deposit_amount"]) == 25.0
        assert b.get("deposit_required") is False

    def test_paypal_me_gives_pending(self, auth_headers, a_shop):
        requests.put(f"{API}/settings", headers=auth_headers,
                     json={"payment_method": "paypal_me", "paypal_me_url": "https://paypal.me/testshop"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=auth_headers,
                       json={"deposit_amount": 30.0, "deposit_required": True}, timeout=15)
        atype = self._atype(a_shop["id"])
        b = self._make_booking(a_shop["id"], atype)
        assert b["payment_status"] == "pending"
        assert float(b["deposit_amount"]) == 30.0
        assert b.get("deposit_required") is True
        # Store for downstream tests
        pytest.paypal_me_ref = b["reference"]
        pytest.paypal_me_booking_id = b["id"]

    def test_zero_deposit_gives_not_required(self, auth_headers, a_shop):
        requests.put(f"{API}/settings", headers=auth_headers, json={"payment_method": "paypal_me"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=auth_headers,
                       json={"deposit_amount": 0, "deposit_required": False}, timeout=15)
        atype = self._atype(a_shop["id"])
        b = self._make_booking(a_shop["id"], atype)
        assert b["payment_status"] == "not_required"


# ---------- Public endpoints on a booking ----------
class TestBookingPaymentEndpoints:
    def test_pay_in_person_endpoint(self, auth_headers, a_shop):
        # ensure paypal_me state so booking becomes pending
        requests.put(f"{API}/settings", headers=auth_headers,
                     json={"payment_method": "paypal_me", "paypal_me_url": "https://paypal.me/testshop"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=auth_headers,
                       json={"deposit_amount": 20.0, "deposit_required": False}, timeout=15)
        atype = requests.get(f"{API}/shops/{a_shop['id']}/appointment-types").json()[0]
        d, slot = _find_open_slot(a_shop["id"], atype["id"], atype["duration"])
        payload = {"shop_id": a_shop["id"], "appointment_type_id": atype["id"],
                   "date": d, "start_time": slot, "customer_name": "TEST_PIP",
                   "customer_email": "t_pip@example.com", "customer_phone": "07000000000",
                   "notes": "", "answers": []}
        b = requests.post(f"{API}/public/bookings", json=payload, timeout=20).json()
        ref = b["reference"]
        assert b["payment_status"] == "pending"

        r = requests.post(f"{API}/public/bookings/{ref}/pay-in-person", timeout=15)
        assert r.status_code == 200, r.text
        got = requests.get(f"{API}/public/bookings/{ref}").json()
        assert got["payment_status"] == "pay_in_person"

    def test_paypal_create_order_400_when_unconfigured(self, auth_headers, a_shop):
        # ensure a fresh pending booking
        requests.put(f"{API}/settings", headers=auth_headers,
                     json={"payment_method": "paypal_me", "paypal_me_url": "https://paypal.me/testshop"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=auth_headers,
                       json={"deposit_amount": 15.0, "deposit_required": True}, timeout=15)
        atype = requests.get(f"{API}/shops/{a_shop['id']}/appointment-types").json()[0]
        d, slot = _find_open_slot(a_shop["id"], atype["id"], atype["duration"])
        b = requests.post(f"{API}/public/bookings", json={
            "shop_id": a_shop["id"], "appointment_type_id": atype["id"], "date": d,
            "start_time": slot, "customer_name": "TEST_PP", "customer_email": "t_pp@example.com",
            "customer_phone": "07000000000", "notes": "", "answers": []}, timeout=20).json()
        r = requests.post(f"{API}/public/bookings/{b['reference']}/paypal/create-order", timeout=15)
        assert r.status_code == 400
        assert "not configured" in r.text.lower()


# ---------- Admin mark paid/unpaid ----------
class TestAdminMarkPaid:
    def test_mark_paid_then_unpaid(self, auth_headers, a_shop):
        # create a pending booking
        requests.put(f"{API}/settings", headers=auth_headers,
                     json={"payment_method": "paypal_me", "paypal_me_url": "https://paypal.me/testshop"}, timeout=15)
        requests.patch(f"{API}/shops/{a_shop['id']}", headers=auth_headers,
                       json={"deposit_amount": 40.0, "deposit_required": True}, timeout=15)
        atype = requests.get(f"{API}/shops/{a_shop['id']}/appointment-types").json()[0]
        d, slot = _find_open_slot(a_shop["id"], atype["id"], atype["duration"])
        b = requests.post(f"{API}/public/bookings", json={
            "shop_id": a_shop["id"], "appointment_type_id": atype["id"], "date": d,
            "start_time": slot, "customer_name": "TEST_MP", "customer_email": "t_mp@example.com",
            "customer_phone": "07000000000", "notes": "", "answers": []}, timeout=20).json()
        bid = b["id"]

        # PATCH mark paid
        r = requests.patch(f"{API}/bookings/{bid}", headers=auth_headers,
                           json={"payment_status": "paid"}, timeout=15)
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["payment_status"] == "paid"
        assert got.get("paid_at"), "paid_at should be set"

        # Revert to pending
        r2 = requests.patch(f"{API}/bookings/{bid}", headers=auth_headers,
                            json={"payment_status": "pending"}, timeout=15)
        assert r2.status_code == 200
        assert r2.json()["payment_status"] == "pending"

    def test_invalid_payment_status_rejected(self, auth_headers):
        # Grab any booking id
        bookings = requests.get(f"{API}/bookings", headers=auth_headers, timeout=15).json()
        if not bookings:
            pytest.skip("no bookings")
        r = requests.patch(f"{API}/bookings/{bookings[0]['id']}", headers=auth_headers,
                           json={"payment_status": "bogus"}, timeout=15)
        assert r.status_code == 400


# ---------- CSV export contains Deposit + Payment ----------
class TestCSVExport:
    def test_csv_has_payment_columns(self, auth_headers):
        r = requests.get(f"{API}/bookings/export.csv", headers=auth_headers, timeout=20)
        assert r.status_code == 200, r.text
        header = r.text.splitlines()[0].lower()
        assert "deposit" in header
        assert "payment" in header
