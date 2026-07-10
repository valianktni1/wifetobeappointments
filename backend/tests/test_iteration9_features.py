"""Iteration 9: Test analytics, no-show, follow-up (series), customers, 'source' question."""
import os
import time
import requests
import pytest
from pathlib import Path

def _load_frontend_url():
    if os.environ.get("REACT_APP_BACKEND_URL"):
        return os.environ["REACT_APP_BACKEND_URL"]
    envp = Path("/app/frontend/.env")
    for line in envp.read_text().splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("REACT_APP_BACKEND_URL not found")

BASE = _load_frontend_url().rstrip("/") + "/api"
EMAIL = "superadmin@wifetobe.co.uk"
PASSWORD = "WifeToBe2026!"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def h(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def shops(h):
    r = requests.get(f"{BASE}/shops", headers=h)
    assert r.status_code == 200
    return r.json()


# ---------------------------------- 'source' question seeded on both shops
def test_source_question_seeded_on_all_shops(shops):
    assert len(shops) >= 2
    for s in shops:
        qs = s.get("questions") or []
        assert any(q.get("id") == "source" or q.get("label") == "How did you hear about us?" for q in qs), \
            f"shop {s['name']} missing source question: {qs}"


# ---------------------------------- Analytics
def test_analytics_shape(h):
    r = requests.get(f"{BASE}/analytics", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ["total", "by_weekday", "by_hour", "by_shop", "by_source", "completed", "no_show", "no_show_rate"]:
        assert key in d, f"missing {key}"
    assert len(d["by_weekday"]) == 7
    days = [x["day"] for x in d["by_weekday"]]
    assert days == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    assert isinstance(d["no_show_rate"], int)


def test_analytics_filter_by_shop(h, shops):
    sid = shops[0]["id"]
    r = requests.get(f"{BASE}/analytics", headers=h, params={"shop_id": sid})
    assert r.status_code == 200
    d = r.json()
    # by_shop entries should only contain that shop (or none if no data)
    for row in d["by_shop"]:
        assert row["shop"] == shops[0]["name"]


# ---------------------------------- Customers
def test_customers_list_and_detail(h):
    r = requests.get(f"{BASE}/customers", headers=h)
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    # If we have a customer, verify detail endpoint too
    if rows:
        em = rows[0]["email"]
        for k in ["email", "name", "phone", "total", "last_date"]:
            assert k in rows[0]
        r2 = requests.get(f"{BASE}/customers/{em}", headers=h)
        assert r2.status_code == 200, r2.text
        d = r2.json()
        assert d["email"] == em
        assert d["total"] >= 1
        assert isinstance(d["bookings"], list) and len(d["bookings"]) == d["total"]


def test_customers_search(h):
    r = requests.get(f"{BASE}/customers", headers=h, params={"q": "zzzz_nomatch_xyz"})
    assert r.status_code == 200
    assert r.json() == []


def test_customer_detail_404(h):
    r = requests.get(f"{BASE}/customers/no-such-email@example.com", headers=h)
    assert r.status_code == 404


# ---------------------------------- Booking create + no-show + follow-up
def _find_open_slot(h, shop_id, atype_id, duration):
    """Iterate next 45 days to find a date with available slots."""
    from datetime import date, timedelta
    today = date.today()
    for i in range(1, 60):
        d = (today + timedelta(days=i)).isoformat()
        r = requests.get(f"{BASE}/public/slots",
                         params={"shop_id": shop_id, "date": d, "duration": duration})
        if r.status_code == 200:
            slots = r.json().get("slots", [])
            if slots:
                return d, slots
    return None, None


@pytest.fixture(scope="module")
def created_booking(h, shops):
    shop = shops[0]
    r = requests.get(f"{BASE}/shops/{shop['id']}/appointment-types", headers=h)
    assert r.status_code == 200
    atypes = r.json()
    assert atypes, "No appointment types"
    atype = atypes[0]
    d, slots = _find_open_slot(h, shop["id"], atype["id"], atype["duration"])
    assert d, "No available slot found in next 60 days"
    payload = {
        "shop_id": shop["id"],
        "appointment_type_id": atype["id"],
        "date": d,
        "start_time": slots[0],
        "customer_name": "TEST_Iter9 Customer",
        "customer_email": "test_iter9@example.com",
        "customer_phone": "07000000009",
        "notes": "iter9 test booking",
        "answers": [{"id": "source", "label": "How did you hear about us?", "value": "Instagram"}],
    }
    r = requests.post(f"{BASE}/public/bookings", json=payload)
    assert r.status_code == 200, r.text
    booking = r.json()
    yield booking, shop, atype, atypes
    # cleanup: cancel any bookings for this customer
    r = requests.get(f"{BASE}/bookings", headers=h)
    if r.status_code == 200:
        for b in r.json():
            if b.get("customer_email") == "test_iter9@example.com":
                requests.patch(f"{BASE}/bookings/{b['id']}", headers=h, json={"status": "cancelled"})


def test_answers_persisted_on_booking(h, created_booking):
    b, _, _, _ = created_booking
    r = requests.get(f"{BASE}/bookings", headers=h)
    assert r.status_code == 200
    found = next((x for x in r.json() if x["id"] == b["id"]), None)
    assert found, "created booking not in admin list"
    ans = found.get("answers") or []
    assert any(a.get("label") == "How did you hear about us?" and a.get("value") == "Instagram" for a in ans), ans


def test_bookings_status_filter_no_show(h):
    r = requests.get(f"{BASE}/bookings", headers=h, params={"status": "no_show"})
    assert r.status_code == 200
    for b in r.json():
        assert b["status"] == "no_show"


def test_patch_booking_to_no_show(h, created_booking):
    b, _, _, _ = created_booking
    r = requests.patch(f"{BASE}/bookings/{b['id']}", headers=h, json={"status": "no_show"})
    assert r.status_code == 200, r.text
    r2 = requests.get(f"{BASE}/bookings", headers=h)
    found = next((x for x in r2.json() if x["id"] == b["id"]), None)
    assert found and found["status"] == "no_show"
    # revert to confirmed for follow-up test
    requests.patch(f"{BASE}/bookings/{b['id']}", headers=h, json={"status": "confirmed"})


def test_analytics_source_reflects_answer(h):
    # After creating booking with 'Instagram', by_source should include it
    r = requests.get(f"{BASE}/analytics", headers=h)
    assert r.status_code == 200
    src = r.json()["by_source"]
    assert any(row["source"] == "Instagram" for row in src), src


def test_follow_up_creates_series(h, created_booking):
    b, shop, atype, atypes = created_booking
    # find another open slot different from parent
    d2, slots2 = _find_open_slot(h, shop["id"], atype["id"], atype["duration"])
    assert d2
    # pick a slot far from parent
    chosen_time = slots2[-1]  # last slot of that day
    r = requests.post(
        f"{BASE}/bookings/{b['id']}/follow-up",
        headers=h,
        json={
            "date": d2,
            "start_time": chosen_time,
            "appointment_type_id": atype["id"],
            "label": "2nd fitting",
        },
    )
    assert r.status_code == 200, r.text
    fu = r.json()
    assert fu["status"] == "confirmed"
    assert fu["customer_email"] == b["customer_email"]
    assert fu.get("series_id"), fu

    # series lists both
    r2 = requests.get(f"{BASE}/bookings/{b['id']}/series", headers=h)
    assert r2.status_code == 200
    series = r2.json()
    assert len(series) >= 2
    ids = [s["id"] for s in series]
    assert b["id"] in ids and fu["id"] in ids


def test_follow_up_invalid_slot_returns_409(h, created_booking):
    b, _, _, _ = created_booking
    r = requests.post(
        f"{BASE}/bookings/{b['id']}/follow-up",
        headers=h,
        json={
            "date": "2099-01-01",
            "start_time": "03:00",
            "appointment_type_id": b["appointment_type_id"],
            "label": "bad",
        },
    )
    assert r.status_code == 409, (r.status_code, r.text)


def test_follow_up_parent_not_found(h):
    r = requests.post(
        f"{BASE}/bookings/000000000000000000000000/follow-up",
        headers=h,
        json={"date": "2099-01-01", "start_time": "10:00"},
    )
    assert r.status_code in (404, 400)


# ---------------------------------- Regression: booking status transitions still work
def test_regression_confirm_complete_cancel(h, created_booking):
    b, _, _, _ = created_booking
    for st in ("confirmed", "completed", "cancelled"):
        r = requests.patch(f"{BASE}/bookings/{b['id']}", headers=h, json={"status": st})
        assert r.status_code == 200, (st, r.text)


def test_regression_csv_export(h):
    r = requests.get(f"{BASE}/bookings/export.csv", headers=h)
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    assert r.text.startswith("Reference,")
