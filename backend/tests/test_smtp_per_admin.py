"""Per-admin SMTP / email-settings endpoint tests.

Covers:
- GET/PUT /api/auth/my-email-settings behavior (masking, defaults, no overwrite on ********)
- POST /api/auth/my-email-settings/test (400 unconfigured, 400 bogus host)
- SECURITY: no endpoint returning user objects leaks smtp_password
- Per-admin isolation between superadmin and a fresh TEST admin
- Regression: update-profile, change-password, 2fa/setup, superadmin login
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
SUPER_EMAIL = "superadmin@wifetobe.co.uk"
SUPER_PW = "WifeToBe2026!"
TEST_ADMIN_EMAIL = "test_smtp_admin@wifetobe.co.uk"
TEST_ADMIN_PW = "TestAdmin!2026"

UA = {"User-Agent": "Mozilla/5.0 (pytest smtp suite)"}


def _sess():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", **UA})
    return s


def _login(sess, email, password):
    r = sess.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text}"
    tok = r.json()["access_token"]
    sess.headers.update({"Authorization": f"Bearer {tok}"})
    return r.json()


@pytest.fixture(scope="module")
def super_sess():
    s = _sess()
    _login(s, SUPER_EMAIL, SUPER_PW)
    yield s
    # teardown: clear any smtp settings we set on superadmin
    try:
        s.put(f"{BASE_URL}/api/auth/my-email-settings", json={
            "smtp_host": "", "smtp_port": 587, "smtp_user": "",
            "smtp_password": "", "sender_email": SUPER_EMAIL, "sender_name": "Wife To Be",
        })
    except Exception:
        pass


@pytest.fixture(scope="module")
def test_admin(super_sess):
    """Create a TEST admin via superadmin, yield (sess_for_admin, admin_id), then delete."""
    # If leftover from previous run, delete
    r = super_sess.get(f"{BASE_URL}/api/admins")
    if r.status_code == 200:
        for a in r.json():
            if a.get("email") == TEST_ADMIN_EMAIL:
                super_sess.delete(f"{BASE_URL}/api/admins/{a['id']}")
    r = super_sess.post(f"{BASE_URL}/api/admins", json={
        "name": "Test SMTP Admin", "email": TEST_ADMIN_EMAIL,
        "password": TEST_ADMIN_PW, "role": "admin",
    })
    assert r.status_code in (200, 201), f"create admin failed: {r.status_code} {r.text}"
    admin_id = r.json()["id"]
    admin_sess = _sess()
    _login(admin_sess, TEST_ADMIN_EMAIL, TEST_ADMIN_PW)
    yield admin_sess, admin_id
    try:
        super_sess.delete(f"{BASE_URL}/api/admins/{admin_id}")
    except Exception:
        pass


# --- GET/PUT /my-email-settings basics ---

class TestMyEmailSettings:
    def test_get_defaults_for_super(self, super_sess):
        r = super_sess.get(f"{BASE_URL}/api/auth/my-email-settings")
        assert r.status_code == 200
        d = r.json()
        for k in ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "sender_email", "sender_name"]:
            assert k in d, f"missing key {k}"
        # sender_email defaults to login email when unset
        assert d["sender_email"] == SUPER_EMAIL or d["sender_email"]  # non-empty at minimum
        # password should be empty when unset (or ******** if leftover) -- never a plaintext value
        assert d["smtp_password"] in ("", "********")

    def test_put_persists_and_password_is_masked_on_get(self, super_sess):
        payload = {
            "smtp_host": "smtp.example-bogus.invalid",
            "smtp_port": 2525,
            "smtp_user": "someone@example.com",
            "smtp_password": "s3cret-real-pw",
            "sender_email": "Hello@Example.COM",
            "sender_name": "Bogus Bridal",
        }
        r = super_sess.put(f"{BASE_URL}/api/auth/my-email-settings", json=payload)
        assert r.status_code == 200, r.text
        # GET -> masked
        g = super_sess.get(f"{BASE_URL}/api/auth/my-email-settings").json()
        assert g["smtp_host"] == "smtp.example-bogus.invalid"
        assert g["smtp_port"] == 2525
        assert g["smtp_user"] == "someone@example.com"
        assert g["smtp_password"] == "********"
        assert g["sender_email"] == "hello@example.com"  # lowercased
        assert g["sender_name"] == "Bogus Bridal"

    def test_put_with_mask_does_not_overwrite_password(self, super_sess):
        # send **** as password -> must keep existing
        r = super_sess.put(f"{BASE_URL}/api/auth/my-email-settings", json={
            "smtp_host": "smtp.example-bogus.invalid",
            "smtp_port": 2525,
            "smtp_user": "someone@example.com",
            "smtp_password": "********",
            "sender_email": "hello@example.com",
            "sender_name": "Bogus Bridal",
        })
        assert r.status_code == 200
        g = super_sess.get(f"{BASE_URL}/api/auth/my-email-settings").json()
        assert g["smtp_password"] == "********"  # still set (not cleared)

    def test_test_endpoint_with_bogus_host_returns_400(self, super_sess):
        r = super_sess.post(f"{BASE_URL}/api/auth/my-email-settings/test", json={"to": "sink@example.com"})
        # SMTP will fail on bogus host -> 400 "Could not send..."
        assert r.status_code == 400
        assert "send" in r.text.lower() or "smtp" in r.text.lower()


# --- SECURITY: no leak of smtp_password ---

class TestNoPasswordLeak:
    def test_auth_me_no_smtp_password(self, super_sess):
        r = super_sess.get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 200
        assert "smtp_password" not in r.json(), "smtp_password leaked in /auth/me"

    def test_admins_list_no_smtp_password(self, super_sess):
        r = super_sess.get(f"{BASE_URL}/api/admins")
        assert r.status_code == 200
        for a in r.json():
            assert "smtp_password" not in a, f"smtp_password leaked in /admins for {a.get('email')}"

    def test_update_profile_no_smtp_password(self, super_sess):
        # get current name/email first
        me = super_sess.get(f"{BASE_URL}/api/auth/me").json()
        r = super_sess.post(f"{BASE_URL}/api/auth/update-profile", json={
            "name": me["name"], "email": me["email"],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        # response may be {ok:true} or user object; either way must not contain smtp_password
        assert "smtp_password" not in body


# --- Per-admin isolation ---

class TestIsolation:
    def test_new_admin_sees_no_smtp_and_email_defaults_to_login(self, test_admin):
        sess, _ = test_admin
        r = sess.get(f"{BASE_URL}/api/auth/my-email-settings")
        assert r.status_code == 200
        d = r.json()
        assert d["smtp_host"] == ""
        assert d["smtp_password"] == ""
        assert d["sender_email"] == TEST_ADMIN_EMAIL

    def test_admin_test_endpoint_400_when_unconfigured(self, test_admin):
        sess, _ = test_admin
        r = sess.post(f"{BASE_URL}/api/auth/my-email-settings/test", json={"to": "sink@example.com"})
        assert r.status_code == 400
        assert "save" in r.text.lower() or "smtp" in r.text.lower() or "host" in r.text.lower()

    def test_admin_can_set_own_smtp_without_affecting_super(self, super_sess, test_admin):
        sess, _ = test_admin
        # capture super's current state
        super_before = super_sess.get(f"{BASE_URL}/api/auth/my-email-settings").json()
        r = sess.put(f"{BASE_URL}/api/auth/my-email-settings", json={
            "smtp_host": "smtp.admin-only.invalid",
            "smtp_port": 465,
            "smtp_user": "admin-user",
            "smtp_password": "admin-only-pw",
            "sender_email": TEST_ADMIN_EMAIL,
            "sender_name": "Test Admin Shop",
        })
        assert r.status_code == 200, r.text
        admin_get = sess.get(f"{BASE_URL}/api/auth/my-email-settings").json()
        assert admin_get["smtp_host"] == "smtp.admin-only.invalid"
        assert admin_get["smtp_port"] == 465
        # super unchanged
        super_after = super_sess.get(f"{BASE_URL}/api/auth/my-email-settings").json()
        assert super_after["smtp_host"] == super_before["smtp_host"]
        assert super_after["smtp_user"] == super_before["smtp_user"]


# --- Regression: existing endpoints still work ---

class TestRegression:
    def test_super_login(self):
        s = _sess()
        r = s.post(f"{BASE_URL}/api/auth/login", json={"email": SUPER_EMAIL, "password": SUPER_PW})
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_update_profile_roundtrip(self, super_sess):
        me = super_sess.get(f"{BASE_URL}/api/auth/me").json()
        original_name = me["name"]
        r = super_sess.post(f"{BASE_URL}/api/auth/update-profile", json={
            "name": original_name + " QA",
            "email": me["email"],
        })
        assert r.status_code == 200
        me2 = super_sess.get(f"{BASE_URL}/api/auth/me").json()
        assert me2["name"] == original_name + " QA"
        # revert
        super_sess.post(f"{BASE_URL}/api/auth/update-profile", json={
            "name": original_name, "email": me["email"],
        })

    def test_change_password_validates_current(self, super_sess):
        r = super_sess.post(f"{BASE_URL}/api/auth/change-password", json={
            "current_password": "wrong-pw-xxx", "new_password": "NewOne!2026",
        })
        assert r.status_code in (400, 401, 403)

    def test_2fa_setup_returns_qr(self, super_sess):
        r = super_sess.post(f"{BASE_URL}/api/auth/2fa/setup")
        assert r.status_code == 200
        d = r.json()
        assert "secret" in d and "qr" in d
