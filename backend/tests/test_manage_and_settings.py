"""Tests for new self-service manage-booking endpoints and new settings fields."""
import os
from datetime import date, timedelta

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://shop-scheduler-17.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

SUPERADMIN_EMAIL = "superadmin@wifetobe.co.uk"
SUPERADMIN_PASSWORD = "WifeToBe2026!"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def super_headers(session):
    r = session.post(f"{API}/auth/login", json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def warrington(session):
    r = session.get(f"{API}/shops")
    return next(s for s in r.json() if "warrington" in s["slug"].lower())


def next_weekday(target: int, offset_weeks: int = 0) -> date:
    b = date.today()
    days = (target - b.weekday()) % 7
    if days == 0:
        days = 7
    return b + timedelta(days=days + offset_weeks * 7)


def _create_booking(session, warrington, when: date, name_suffix="A"):
    types = session.get(f"{API}/shops/{warrington['id']}/appointment-types").json()
    t60 = next(t for t in types if t["duration"] == 60)
    slots = session.get(f"{API}/public/slots", params={
        "shop_id": warrington["id"], "date": when.isoformat(), "duration": 60
    }).json()["slots"]
    assert slots
    payload = {
        "shop_id": warrington["id"], "appointment_type_id": t60["id"],
        "date": when.isoformat(), "start_time": slots[0],
        "customer_name": f"TEST_Mgr_{name_suffix}",
        "customer_email": f"test_mgr_{name_suffix.lower()}@example.com",
        "customer_phone": "01234567890", "notes": "TEST",
    }
    r = session.post(f"{API}/public/bookings", json=payload)
    assert r.status_code == 200, r.text
    return r.json(), t60, slots[0]


# -------- teardown ---------
_created_refs = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup(session, super_headers):
    yield
    for ref in _created_refs:
        try:
            b = session.get(f"{API}/public/bookings/{ref}").json()
            requests.patch(f"{API}/bookings/{b['id']}", headers=super_headers, json={"status": "cancelled"})
        except Exception:
            pass


class TestPublicGetByReference:
    def test_get_existing(self, session, warrington):
        d = next_weekday(1)
        b, _, _ = _create_booking(session, warrington, d, name_suffix="Get")
        _created_refs.append(b["reference"])
        r = session.get(f"{API}/public/bookings/{b['reference']}")
        assert r.status_code == 200
        assert r.json()["reference"] == b["reference"]

    def test_get_nonexistent(self, session):
        r = session.get(f"{API}/public/bookings/NOPE-DOES-NOT-EXIST")
        assert r.status_code == 404


class TestPublicReschedule:
    def test_reschedule_to_valid_slot(self, session, warrington):
        d1 = next_weekday(1)  # next Tuesday
        b, _, orig_slot = _create_booking(session, warrington, d1, name_suffix="Resch")
        _created_refs.append(b["reference"])

        d2 = next_weekday(1, offset_weeks=1)  # Tuesday after
        slots = session.get(f"{API}/public/slots", params={
            "shop_id": warrington["id"], "date": d2.isoformat(), "duration": b["duration"]
        }).json()["slots"]
        new_slot = slots[2]
        r = session.post(f"{API}/public/bookings/{b['reference']}/reschedule",
                         json={"date": d2.isoformat(), "start_time": new_slot})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["date"] == d2.isoformat()
        assert d["start_time"] == new_slot
        assert d["status"] == "pending"

        # verify persistence via GET
        r2 = session.get(f"{API}/public/bookings/{b['reference']}")
        assert r2.json()["date"] == d2.isoformat()
        assert r2.json()["start_time"] == new_slot

    def test_reschedule_nonexistent_returns_404(self, session):
        r = session.post(f"{API}/public/bookings/WTB-NOPE-XXX/reschedule",
                         json={"date": next_weekday(1).isoformat(), "start_time": "11:00"})
        assert r.status_code == 404

    def test_reschedule_to_occupied_slot_returns_409(self, session, warrington):
        d = next_weekday(1, offset_weeks=2)
        a, _, a_slot = _create_booking(session, warrington, d, name_suffix="OccA")
        _created_refs.append(a["reference"])
        b, _, b_slot = _create_booking(session, warrington, d, name_suffix="OccB")
        _created_refs.append(b["reference"])
        assert a_slot != b_slot

        # Try to reschedule B onto A's slot -> 409
        r = session.post(f"{API}/public/bookings/{b['reference']}/reschedule",
                         json={"date": d.isoformat(), "start_time": a_slot})
        assert r.status_code == 409

    def test_reschedule_to_own_current_slot_ok(self, session, warrington):
        """exclude_ref: B can be 'rescheduled' onto its own current slot."""
        d = next_weekday(1, offset_weeks=3)
        b, _, b_slot = _create_booking(session, warrington, d, name_suffix="Self")
        _created_refs.append(b["reference"])
        r = session.post(f"{API}/public/bookings/{b['reference']}/reschedule",
                         json={"date": d.isoformat(), "start_time": b_slot})
        assert r.status_code == 200, r.text
        assert r.json()["start_time"] == b_slot


class TestPublicCancel:
    def test_cancel_flow_and_slot_frees(self, session, warrington):
        d = next_weekday(1, offset_weeks=4)
        b, _, b_slot = _create_booking(session, warrington, d, name_suffix="Cxl")
        _created_refs.append(b["reference"])

        # Slot should not be in availability
        slots_before = session.get(f"{API}/public/slots", params={
            "shop_id": warrington["id"], "date": d.isoformat(), "duration": b["duration"]
        }).json()["slots"]
        assert b_slot not in slots_before

        # Cancel
        r = session.post(f"{API}/public/bookings/{b['reference']}/cancel")
        assert r.status_code == 200

        # Slot should be free again
        slots_after = session.get(f"{API}/public/slots", params={
            "shop_id": warrington["id"], "date": d.isoformat(), "duration": b["duration"]
        }).json()["slots"]
        assert b_slot in slots_after

        # Status is cancelled
        got = session.get(f"{API}/public/bookings/{b['reference']}").json()
        assert got["status"] == "cancelled"

        # Cancelling again -> 400
        r = session.post(f"{API}/public/bookings/{b['reference']}/cancel")
        assert r.status_code == 400

        # Rescheduling a cancelled booking -> 400
        r = session.post(f"{API}/public/bookings/{b['reference']}/reschedule",
                         json={"date": d.isoformat(), "start_time": b_slot})
        assert r.status_code == 400

    def test_cancel_nonexistent(self, session):
        r = session.post(f"{API}/public/bookings/WTB-NOPE-XXX/cancel")
        assert r.status_code == 404


class TestSettingsNewFields:
    def test_a_put_persists_new_fields(self, session, super_headers):
        url = "https://example-test.wifetobe.co.uk"
        r = session.put(f"{API}/settings", headers=super_headers,
                        json={"public_url": url, "notify_reminder": True})
        assert r.status_code == 200
        r = session.get(f"{API}/settings", headers=super_headers)
        d = r.json()
        assert d["public_url"] == url
        assert d["notify_reminder"] is True

        # toggle back
        r = session.put(f"{API}/settings", headers=super_headers,
                        json={"notify_reminder": False})
        assert r.status_code == 200
        d = session.get(f"{API}/settings", headers=super_headers).json()
        assert d["notify_reminder"] is False

    def test_b_get_includes_new_fields(self, session, super_headers):
        r = session.get(f"{API}/settings", headers=super_headers)
        assert r.status_code == 200
        d = r.json()
        assert "public_url" in d
        assert "notify_reminder" in d
        assert d["smtp_password"] in ("", "********")


class TestRegression:
    def test_login_and_shops(self, session):
        r = session.post(f"{API}/auth/login", json={"email": SUPERADMIN_EMAIL, "password": SUPERADMIN_PASSWORD})
        assert r.status_code == 200
        assert "access_token" in r.json()

        r = session.get(f"{API}/shops")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert any("Warrington" in n for n in names)
        assert any("Runcorn" in n for n in names)

    def test_warrington_hours(self, session, warrington):
        tue = next_weekday(1, offset_weeks=5)  # far future to avoid test-created bookings
        r = session.get(f"{API}/public/slots", params={
            "shop_id": warrington["id"], "date": tue.isoformat(), "duration": 60
        })
        slots = r.json()["slots"]
        assert "11:00" in slots  # opens at 11 on Tuesday

        mon = next_weekday(0)
        r = session.get(f"{API}/public/slots", params={
            "shop_id": warrington["id"], "date": mon.isoformat(), "duration": 60
        })
        assert r.json()["slots"] == []  # closed Monday

        sat = next_weekday(5, offset_weeks=5)
        r = session.get(f"{API}/public/slots", params={
            "shop_id": warrington["id"], "date": sat.isoformat(), "duration": 60
        })
        assert "10:00" in r.json()["slots"]  # opens at 10 on Saturday
