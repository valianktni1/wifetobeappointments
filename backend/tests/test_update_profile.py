"""Tests for POST /api/auth/update-profile (self-service admin profile changes)."""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"

SUPER_EMAIL = "superadmin@wifetobe.co.uk"
SUPER_PASSWORD = "WifeToBe2026!"


def _login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password})
    return r


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def super_token():
    r = _login(SUPER_EMAIL, SUPER_PASSWORD)
    assert r.status_code == 200, f"Superadmin login failed: {r.text}"
    return r.json()["access_token"]


@pytest.fixture
def test_admin(super_token):
    """Create a fresh TEST_ admin, yield (id, email, password, token), cleanup after."""
    suffix = uuid.uuid4().hex[:8]
    email = f"test_{suffix}@example.com"
    password = "TestPass123!"
    name = f"TEST_{suffix}"
    r = requests.post(
        f"{API}/admins",
        headers=_auth_headers(super_token),
        json={"name": name, "email": email, "password": password},
    )
    assert r.status_code in (200, 201), f"Admin create failed: {r.status_code} {r.text}"
    admin = r.json()
    admin_id = admin.get("id") or admin.get("_id")

    # login as this admin
    lr = _login(email, password)
    assert lr.status_code == 200, f"New admin login failed: {lr.text}"
    token = lr.json()["access_token"]

    yield {"id": admin_id, "email": email, "password": password, "token": token, "name": name}

    # teardown
    requests.delete(f"{API}/admins/{admin_id}", headers=_auth_headers(super_token))


class TestUpdateProfile:
    def test_update_name_only(self, test_admin):
        new_name = f"{test_admin['name']}_renamed"
        r = requests.post(
            f"{API}/auth/update-profile",
            headers=_auth_headers(test_admin["token"]),
            json={"name": new_name},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["name"] == new_name
        assert data["email"] == test_admin["email"]
        assert "password_hash" not in data
        assert "totp_secret" not in data

        me = requests.get(f"{API}/auth/me", headers=_auth_headers(test_admin["token"]))
        assert me.status_code == 200
        assert me.json()["name"] == new_name

    def test_update_email_and_login_with_new_email(self, test_admin):
        new_email = f"test_new_{uuid.uuid4().hex[:8]}@example.com"
        r = requests.post(
            f"{API}/auth/update-profile",
            headers=_auth_headers(test_admin["token"]),
            json={"email": new_email},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["email"] == new_email.lower()
        assert "password_hash" not in data

        # login with new email works
        lr_new = _login(new_email, test_admin["password"])
        assert lr_new.status_code == 200, f"Login with new email failed: {lr_new.text}"

        # old email no longer logs in
        lr_old = _login(test_admin["email"], test_admin["password"])
        assert lr_old.status_code in (400, 401), f"Expected old email to fail login, got {lr_old.status_code}"

    def test_email_lowercased(self, test_admin):
        mixed = f"MiXeD_{uuid.uuid4().hex[:6]}@Example.Com"
        r = requests.post(
            f"{API}/auth/update-profile",
            headers=_auth_headers(test_admin["token"]),
            json={"email": mixed},
        )
        assert r.status_code == 200
        assert r.json()["email"] == mixed.lower()

    def test_email_conflict_returns_400(self, super_token, test_admin):
        # Try to change test admin email to superadmin email
        r = requests.post(
            f"{API}/auth/update-profile",
            headers=_auth_headers(test_admin["token"]),
            json={"email": SUPER_EMAIL},
        )
        assert r.status_code == 400
        detail = r.json().get("detail", "")
        assert "already in use" in detail.lower()

    def test_update_own_email_no_op(self, test_admin):
        r = requests.post(
            f"{API}/auth/update-profile",
            headers=_auth_headers(test_admin["token"]),
            json={"email": test_admin["email"]},
        )
        assert r.status_code == 200
        assert r.json()["email"] == test_admin["email"]

    def test_unauthenticated_rejected(self):
        r = requests.post(f"{API}/auth/update-profile", json={"name": "Nope"})
        assert r.status_code in (401, 403)


class TestRegression:
    def test_superadmin_login(self):
        r = _login(SUPER_EMAIL, SUPER_PASSWORD)
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_change_password_still_works(self, test_admin):
        new_pw = "NewPass456!"
        r = requests.post(
            f"{API}/auth/change-password",
            headers=_auth_headers(test_admin["token"]),
            json={"current_password": test_admin["password"], "new_password": new_pw},
        )
        assert r.status_code == 200

        lr = _login(test_admin["email"], new_pw)
        assert lr.status_code == 200

    def test_2fa_setup_endpoint(self, test_admin):
        r = requests.post(f"{API}/auth/2fa/setup", headers=_auth_headers(test_admin["token"]))
        assert r.status_code == 200
        assert "qr" in r.json()
