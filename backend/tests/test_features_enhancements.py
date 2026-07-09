"""Backend tests for the 7 enhancements + regression on core flows."""
import os
import io
from datetime import datetime, timedelta, date

import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if os.environ.get("REACT_APP_BACKEND_URL") else "https://shop-scheduler-17.preview.emergentagent.com"
API = f"{BASE}/api"

SUPER_EMAIL = "superadmin@wifetobe.co.uk"
SUPER_PW = "WifeToBe2026!"


# ---- shared session/state ----
@pytest.fixture(scope="session")
def token():
    r = requests.post(f"{API}/auth/login", json={"email": SUPER_EMAIL, "password": SUPER_PW}, timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "access_token" in data
    return data["access_token"]


@pytest.fixture(scope="session")
def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def shops():
    r = requests.get(f"{API}/shops", timeout=30)
    assert r.status_code == 200
    data = r.json()
    assert len(data) >= 2
    return data


@pytest.fixture(scope="session")
def warrington(shops):
    return next(s for s in shops if "warrington" in s["slug"].lower())


@pytest.fixture(scope="session")
def runcorn(shops):
    return next(s for s in shops if "runcorn" in s["slug"].lower())


def _future_open_date_for(shop, wanted_weekday):
    """Return YYYY-MM-DD string for a future date matching weekday (0=Mon)."""
    today = date.today()
    for i in range(1, 60):
        d = today + timedelta(days=i)
        if d.weekday() == wanted_weekday:
            return d.isoformat()
    raise AssertionError("no future date")


# ============================================================
# REGRESSION: superadmin login + auth me + shops + slots
# ============================================================
class TestRegression:
    def test_login_ok(self, token):
        assert isinstance(token, str) and len(token) > 20

    def test_auth_me(self, auth):
        r = requests.get(f"{API}/auth/me", headers=auth, timeout=30)
        assert r.status_code == 200
        u = r.json()
        assert u["email"] == SUPER_EMAIL
        assert u["role"] == "superadmin"

    def test_shops_list_public(self, shops):
        assert all("id" in s and "name" in s for s in shops)

    def test_shops_have_new_fields(self, warrington):
        # backward compat: fields exist (possibly empty)
        r = requests.get(f"{API}/shops/{warrington['id']}", timeout=30)
        assert r.status_code == 200
        s = r.json()
        # not required to be non-empty; but keys should be present after seed
        assert "questions" in s
        assert "photo_url" in s
        assert "hours_text" in s


# ============================================================
# (12) BOUTIQUE DETAILS: PATCH /shops/{id}
# ============================================================
class TestBoutiqueDetails:
    def test_patch_hours_and_photo(self, auth, warrington):
        payload = {
            "hours_text": "TEST_HOURS Tue-Fri 11-5",
            "photo_url": "https://example.com/test-boutique.jpg",
        }
        r = requests.patch(f"{API}/shops/{warrington['id']}", json=payload, headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        # Verify persistence via public GET
        r2 = requests.get(f"{API}/shops/{warrington['id']}", timeout=30)
        assert r2.status_code == 200
        s = r2.json()
        assert s["hours_text"] == "TEST_HOURS Tue-Fri 11-5"
        assert s["photo_url"] == "https://example.com/test-boutique.jpg"


# ============================================================
# (1) CUSTOM QUESTIONS
# ============================================================
class TestCustomQuestions:
    def test_set_and_get_questions(self, auth, warrington):
        payload = {"questions": [
            {"label": "TEST_Dress size?", "type": "select",
             "options": ["A", "B", "C"], "required": True},
            {"label": "TEST_Any notes", "type": "text", "options": [], "required": False},
        ]}
        r = requests.put(f"{API}/shops/{warrington['id']}/questions", json=payload, headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        qs = r.json()["questions"]
        assert len(qs) == 2
        assert all("id" in q for q in qs)  # ids assigned
        assert qs[0]["required"] is True
        assert qs[0]["options"] == ["A", "B", "C"]

        # Public GET carries questions
        s = requests.get(f"{API}/shops/{warrington['id']}", timeout=30).json()
        assert len(s["questions"]) == 2

    def test_booking_stores_answers(self, auth, warrington):
        # ensure questions set (redo to be independent)
        s = requests.get(f"{API}/shops/{warrington['id']}", timeout=30).json()
        assert s["questions"], "prev test must have set questions"
        atypes = requests.get(f"{API}/shops/{warrington['id']}/appointment-types", timeout=30).json()
        atype = atypes[0]
        # pick date matching Warrington open day (Tue=1)
        d = _future_open_date_for(warrington, 1)
        slots = requests.get(f"{API}/public/slots",
                             params={"shop_id": warrington["id"], "date": d, "duration": atype["duration"]},
                             timeout=30).json()["slots"]
        assert slots, "expected some slots on Warrington Tue"
        t = slots[0]
        ans = [{"label": s["questions"][0]["label"], "value": "A"}]
        book = {
            "shop_id": warrington["id"], "appointment_type_id": atype["id"],
            "date": d, "start_time": t,
            "customer_name": "TEST_CustQ",
            "customer_email": "test_custq@example.com",
            "customer_phone": "07000000000",
            "notes": "TEST",
            "answers": ans,
        }
        r = requests.post(f"{API}/public/bookings", json=book, timeout=30)
        assert r.status_code == 200, r.text
        doc = r.json()
        assert doc["answers"] == ans
        assert doc["shop_address"]  # includes shop_address per booking_to_vevent
        # remember to cleanup: cancel via admin path
        rref = doc["reference"]
        # store on class for later cleanup
        TestCustomQuestions._ref = rref

    @classmethod
    def teardown_class(cls):
        # cleanup booking + reset questions
        try:
            r = requests.post(f"{API}/auth/login", json={"email": SUPER_EMAIL, "password": SUPER_PW}, timeout=10)
            tok = r.json()["access_token"]
            hdr = {"Authorization": f"Bearer {tok}"}
            shops = requests.get(f"{API}/shops").json()
            warr = next(s for s in shops if "warrington" in s["slug"].lower())
            requests.put(f"{API}/shops/{warr['id']}/questions", json={"questions": []}, headers=hdr, timeout=10)
            ref = getattr(cls, "_ref", None)
            if ref:
                # find booking id
                bks = requests.get(f"{API}/bookings", headers=hdr).json()
                match = [b for b in bks if b.get("reference") == ref]
                if match:
                    requests.patch(f"{API}/bookings/{match[0]['id']}", json={"status": "cancelled"}, headers=hdr, timeout=10)
        except Exception:
            pass


# ============================================================
# (2) CAPACITY + BUFFER
# ============================================================
class TestCapacityAndBuffer:
    def test_capacity_allows_two_bookings_at_same_time(self, auth, runcorn):
        # Set capacity=2 buffer=0 on Runcorn, use a future Tue (open)
        # get current availability first to restore later
        avail_before = requests.get(f"{API}/shops/{runcorn['id']}/availability").json()
        hours = avail_before.get("hours") or {}
        # Some fallback if empty
        payload = {"hours": hours, "slot_step": avail_before.get("slot_step", 30),
                   "capacity": 2, "buffer": 0}
        r = requests.put(f"{API}/shops/{runcorn['id']}/availability", json=payload, headers=auth, timeout=30)
        assert r.status_code == 200

        atypes = requests.get(f"{API}/shops/{runcorn['id']}/appointment-types").json()
        atype = atypes[0]
        d = _future_open_date_for(runcorn, 1)  # Tue
        slots = requests.get(f"{API}/public/slots",
                             params={"shop_id": runcorn["id"], "date": d, "duration": atype["duration"]}).json()["slots"]
        assert slots, "need available slots"
        t = slots[0]

        refs = []
        for i in range(2):
            book = {
                "shop_id": runcorn["id"], "appointment_type_id": atype["id"],
                "date": d, "start_time": t,
                "customer_name": f"TEST_Cap{i}",
                "customer_email": f"test_cap{i}@example.com",
                "customer_phone": "07000000000",
            }
            r = requests.post(f"{API}/public/bookings", json=book, timeout=30)
            assert r.status_code == 200, f"booking {i} failed: {r.text}"
            refs.append(r.json()["reference"])

        # third at same time must be rejected
        book3 = {"shop_id": runcorn["id"], "appointment_type_id": atype["id"],
                 "date": d, "start_time": t,
                 "customer_name": "TEST_Cap3",
                 "customer_email": "test_cap3@example.com",
                 "customer_phone": "07000000000"}
        r3 = requests.post(f"{API}/public/bookings", json=book3, timeout=30)
        assert r3.status_code == 409, f"expected 409, got {r3.status_code}: {r3.text}"

        TestCapacityAndBuffer._refs = refs
        TestCapacityAndBuffer._d = d
        TestCapacityAndBuffer._t = t
        TestCapacityAndBuffer._duration = atype["duration"]
        TestCapacityAndBuffer._avail_before = avail_before

    def test_buffer_excludes_overlapping_slots(self, auth, runcorn):
        # Set buffer=15 while capacity=1 (only 1 concurrent) => slot within
        # [start-15..end+15] should not be offered
        avail = requests.get(f"{API}/shops/{runcorn['id']}/availability").json()
        # For clarity, use capacity=1 for this test
        payload = {"hours": avail.get("hours") or {}, "slot_step": avail.get("slot_step", 30),
                   "capacity": 1, "buffer": 15}
        r = requests.put(f"{API}/shops/{runcorn['id']}/availability", json=payload, headers=auth, timeout=30)
        assert r.status_code == 200

        # There is already an existing booking at TestCapacityAndBuffer._t on _d (from prev test),
        # but capacity was 2 then. Now capacity=1 buffer=15. Verify slots exclude the overlapping windows.
        d = TestCapacityAndBuffer._d
        t = TestCapacityAndBuffer._t
        duration = TestCapacityAndBuffer._duration
        slots = requests.get(f"{API}/public/slots",
                             params={"shop_id": runcorn["id"], "date": d, "duration": duration}).json()["slots"]
        # 't' itself must not be offered (already booked, capacity=1)
        assert t not in slots
        # a slot starting +duration - buffer + small before end+buffer should be excluded
        def to_min(s):
            h, m = s.split(":"); return int(h)*60+int(m)
        t_min = to_min(t)
        for s in slots:
            s_min = to_min(s)
            # s must NOT overlap [t_min-15, t_min+duration+15]
            overlap = s_min < t_min + duration + 15 and s_min + duration > t_min - 15
            assert not overlap, f"slot {s} overlaps buffer window around {t}"

    @classmethod
    def teardown_class(cls):
        # cleanup: cancel bookings and reset availability
        try:
            r = requests.post(f"{API}/auth/login", json={"email": SUPER_EMAIL, "password": SUPER_PW}, timeout=10)
            tok = r.json()["access_token"]
            hdr = {"Authorization": f"Bearer {tok}"}
            for ref in getattr(cls, "_refs", []):
                bks = requests.get(f"{API}/bookings", headers=hdr).json()
                match = [b for b in bks if b.get("reference") == ref]
                if match:
                    requests.patch(f"{API}/bookings/{match[0]['id']}", json={"status": "cancelled"}, headers=hdr, timeout=10)
            avb = getattr(cls, "_avail_before", None)
            if avb:
                shops = requests.get(f"{API}/shops").json()
                runc = next(s for s in shops if "runcorn" in s["slug"].lower())
                requests.put(f"{API}/shops/{runc['id']}/availability", json={
                    "hours": avb.get("hours") or {},
                    "slot_step": avb.get("slot_step", 30),
                    "capacity": 1, "buffer": 0,
                }, headers=hdr, timeout=10)
        except Exception:
            pass


# ============================================================
# (4) INTRA-DAY BLOCKS
# ============================================================
class TestPartialDayBlock:
    def test_partial_and_whole_day_blocks(self, auth, warrington):
        d = _future_open_date_for(warrington, 2)  # Wednesday
        # add block 13:00-14:00
        r = requests.post(f"{API}/shops/{warrington['id']}/blocked-dates",
                          json={"date": d, "reason": "TEST_lunch", "start_time": "13:00", "end_time": "14:00"},
                          headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        block_id = r.json()["id"]

        # duration=60 => a slot at 13:00 or 13:30 would overlap 13-14; assert none overlap
        slots = requests.get(f"{API}/public/slots",
                             params={"shop_id": warrington["id"], "date": d, "duration": 60}).json()["slots"]
        assert slots, "expected some slots"
        def to_min(s):
            h, m = s.split(":"); return int(h)*60+int(m)
        for s in slots:
            sm = to_min(s)
            overlap = sm < 14*60 and sm + 60 > 13*60
            assert not overlap, f"slot {s} overlaps partial block 13-14"

        # cleanup this block
        r = requests.delete(f"{API}/blocked-dates/{block_id}", headers=auth, timeout=30)
        assert r.status_code == 200

        # whole day block closes
        d2 = _future_open_date_for(warrington, 3)  # Thu
        r = requests.post(f"{API}/shops/{warrington['id']}/blocked-dates",
                          json={"date": d2, "reason": "TEST_whole"}, headers=auth, timeout=30)
        assert r.status_code == 200
        block_id2 = r.json()["id"]
        slots2 = requests.get(f"{API}/public/slots",
                              params={"shop_id": warrington["id"], "date": d2, "duration": 60}).json()
        assert slots2["slots"] == []
        assert slots2.get("reason") == "closed"
        requests.delete(f"{API}/blocked-dates/{block_id2}", headers=auth, timeout=30)


# ============================================================
# (8) CSV EXPORT
# ============================================================
class TestCsvExport:
    def test_csv_export(self, auth):
        r = requests.get(f"{API}/bookings/export.csv", headers=auth, timeout=30)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("text/csv"), r.headers
        body = r.text
        first_line = body.splitlines()[0]
        assert "Reference" in first_line and "Date" in first_line and "Customer" in first_line

    def test_csv_export_requires_auth(self):
        r = requests.get(f"{API}/bookings/export.csv", timeout=30)
        assert r.status_code == 401


# ============================================================
# (8) iCAL FEED
# ============================================================
class TestIcalFeed:
    def test_ical_feed(self, auth):
        s = requests.get(f"{API}/settings", headers=auth, timeout=30).json()
        token = s.get("feed_token")
        assert token, "feed_token must exist after GET /settings"
        r = requests.get(f"{API}/calendar/{token}.ics", timeout=30)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("text/calendar")
        assert "BEGIN:VCALENDAR" in r.text
        assert "END:VCALENDAR" in r.text

    def test_ical_wrong_token_404(self):
        r = requests.get(f"{API}/calendar/not-a-real-token-xyz.ics", timeout=30)
        assert r.status_code == 404


# ============================================================
# (11) ADD TO CALENDAR (per-booking .ics)
# ============================================================
class TestPublicBookingIcs:
    def test_create_and_get_ics(self, auth, runcorn):
        atypes = requests.get(f"{API}/shops/{runcorn['id']}/appointment-types").json()
        atype = atypes[0]
        d = _future_open_date_for(runcorn, 3)  # Thu
        slots = requests.get(f"{API}/public/slots",
                             params={"shop_id": runcorn["id"], "date": d, "duration": atype["duration"]}).json()["slots"]
        assert slots
        book = {
            "shop_id": runcorn["id"], "appointment_type_id": atype["id"],
            "date": d, "start_time": slots[0],
            "customer_name": "TEST_ICS",
            "customer_email": "test_ics@example.com",
            "customer_phone": "07000000000",
        }
        r = requests.post(f"{API}/public/bookings", json=book, timeout=30)
        assert r.status_code == 200, r.text
        ref = r.json()["reference"]

        r2 = requests.get(f"{API}/public/bookings/{ref}/calendar.ics", timeout=30)
        assert r2.status_code == 200
        assert r2.headers.get("content-type", "").startswith("text/calendar")
        assert "BEGIN:VEVENT" in r2.text
        assert "END:VEVENT" in r2.text

        # cleanup
        bks = requests.get(f"{API}/bookings", headers=auth).json()
        match = [b for b in bks if b.get("reference") == ref]
        if match:
            requests.patch(f"{API}/bookings/{match[0]['id']}", json={"status": "cancelled"}, headers=auth, timeout=10)


# ============================================================
# (15) WAITLIST
# ============================================================
class TestWaitlist:
    def test_add_and_admin_flow(self, auth, warrington):
        # add waitlist entry (public)
        payload = {
            "shop_id": warrington["id"],
            "customer_name": "TEST_Waitlist",
            "customer_email": "test_wait@example.com",
            "customer_phone": "07000000001",
            "notes": "TEST",
        }
        r = requests.post(f"{API}/public/waitlist", json=payload, timeout=30)
        assert r.status_code == 200, r.text
        entry_id = r.json()["id"]
        assert r.json()["status"] == "waiting"

        # admin list
        lst = requests.get(f"{API}/waitlist", headers=auth, timeout=30).json()
        assert any(x["id"] == entry_id for x in lst)

        # mark contacted
        r = requests.patch(f"{API}/waitlist/{entry_id}", json={"status": "contacted"}, headers=auth, timeout=30)
        assert r.status_code == 200
        assert r.json()["status"] == "contacted"

        # delete
        r = requests.delete(f"{API}/waitlist/{entry_id}", headers=auth, timeout=30)
        assert r.status_code == 200

        lst2 = requests.get(f"{API}/waitlist", headers=auth, timeout=30).json()
        assert all(x["id"] != entry_id for x in lst2)

    def test_waitlist_requires_auth_for_admin_endpoints(self):
        r = requests.get(f"{API}/waitlist", timeout=30)
        assert r.status_code == 401


# ============================================================
# BACKWARD COMPAT: create booking without 'answers' and with empty questions
# ============================================================
class TestBackwardCompat:
    def test_booking_without_answers(self, auth, runcorn):
        atypes = requests.get(f"{API}/shops/{runcorn['id']}/appointment-types").json()
        atype = atypes[0]
        d = _future_open_date_for(runcorn, 4)  # Fri
        slots = requests.get(f"{API}/public/slots",
                             params={"shop_id": runcorn["id"], "date": d, "duration": atype["duration"]}).json()["slots"]
        assert slots
        r = requests.post(f"{API}/public/bookings", json={
            "shop_id": runcorn["id"], "appointment_type_id": atype["id"],
            "date": d, "start_time": slots[0],
            "customer_name": "TEST_Compat",
            "customer_email": "test_compat@example.com",
            "customer_phone": "07000000002",
        }, timeout=30)
        assert r.status_code == 200, r.text
        assert r.json().get("answers") == []
        ref = r.json()["reference"]
        # cleanup
        bks = requests.get(f"{API}/bookings", headers=auth).json()
        match = [b for b in bks if b.get("reference") == ref]
        if match:
            requests.patch(f"{API}/bookings/{match[0]['id']}", json={"status": "cancelled"}, headers=auth, timeout=10)
