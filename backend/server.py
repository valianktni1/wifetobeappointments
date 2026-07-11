import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import logging
import asyncio
import smtplib
import secrets
import csv
import uuid
from datetime import datetime, timezone, timedelta, date, time as dtime
from email.message import EmailMessage
from typing import List, Optional, Annotated, Any

import bcrypt
import jwt
import pyotp
import qrcode
import io
import base64
import httpx
from bson import ObjectId
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, Field, BeforeValidator, EmailStr, ConfigDict
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient

# ------------------------------------------------------------------ config
mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALG = "HS256"
MAX_ADMINS = 4  # non-superadmin admins

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()
api = APIRouter(prefix="/api")

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DOCS_DIR = Path(__file__).resolve().parent.parent


@api.get("/download/pitch")
async def download_pitch():
    fp = DOCS_DIR / "WifeToBe-Appointments-The-Plain-English-Pitch.txt"
    if not fp.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(fp), media_type="text/plain", filename=fp.name)


@api.get("/download/overview")
async def download_overview():
    fp = DOCS_DIR / "WifeToBe-Appointments-System-Overview.txt"
    if not fp.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(fp), media_type="text/plain", filename=fp.name)


# ------------------------------------------------------------------ helpers
PyObjectId = Annotated[str, BeforeValidator(str)]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def create_token(user_id: str, ttype: str = "access", minutes: int = 60 * 12) -> str:
    payload = {"sub": user_id, "type": ttype, "exp": now_utc() + timedelta(minutes=minutes)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])


def oid(v: str) -> ObjectId:
    try:
        return ObjectId(v)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")


def clean(doc: dict) -> dict:
    if not doc:
        return doc
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    doc.pop("password_hash", None)
    doc.pop("totp_secret", None)
    doc.pop("smtp_password", None)
    return doc


async def get_current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"_id": oid(payload["sub"])})
        if not user or not user.get("active", True):
            raise HTTPException(status_code=401, detail="User not found or disabled")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def require_superadmin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return user


# ------------------------------------------------------------------ email
SETTINGS_DEFAULTS = {
    "business_email": "",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "from_name": "Wife To Be",
    "public_url": "",
    "feed_token": "",
    "notify_customer_on_booking": False,
    "notify_shop_on_booking": False,
    "notify_on_confirm": False,
    "notify_reminder": False,
    "payment_method": "off",       # off | in_person | paypal_me | paypal
    "paypal_me_url": "",           # e.g. https://paypal.me/yourbusiness
    "payment_currency": "GBP",
}


async def get_settings() -> dict:
    s = await db.settings.find_one({"_id": "global"})
    if not s:
        s = {"_id": "global", **SETTINGS_DEFAULTS}
        await db.settings.insert_one(s)
        return s
    missing = {k: v for k, v in SETTINGS_DEFAULTS.items() if k not in s}
    if missing:
        await db.settings.update_one({"_id": "global"}, {"$set": missing})
        s.update(missing)
    return s


def manage_link(settings: dict, ref: str) -> str:
    base = (settings.get("public_url") or "").rstrip("/")
    return f"\n\nView or reschedule your appointment: {base}/booking/{ref}" if base else ""


def manage_url(settings: dict, ref: str) -> Optional[str]:
    base = (settings.get("public_url") or "").rstrip("/")
    return f"{base}/booking/{ref}" if base else None


LOGO_PATH = ROOT_DIR / "wtb_logo.png"
try:
    _LOGO_BYTES = LOGO_PATH.read_bytes()
except Exception:
    _LOGO_BYTES = None


def render_email(heading: str, paragraphs: List[str], cta: Optional[dict] = None) -> str:
    """Elegant, brand-matched HTML email with the Wife To Be wordmark."""
    logo = ('<img src="cid:wtblogo" alt="Wife To Be" '
            'style="max-width:230px;height:auto;margin:0 auto;display:block;">') if _LOGO_BYTES else \
           ('<div style="font-family:Georgia,\'Times New Roman\',serif;font-size:34px;'
            'color:#b08d57;font-style:italic;">Wife To Be</div>')
    body_html = "".join(
        f'<p style="margin:0 0 16px;font-family:Georgia,serif;font-size:16px;line-height:1.7;color:#3d3833;">{p}</p>'
        for p in paragraphs
    )
    cta_html = ""
    if cta and cta.get("url"):
        cta_html = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px auto 24px;">'
            f'<tr><td style="background:#b08d57;">'
            f'<a href="{cta["url"]}" style="display:inline-block;padding:14px 34px;font-family:Arial,sans-serif;'
            f'font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#fff;text-decoration:none;">'
            f'{cta.get("label","View")}</a></td></tr></table>'
        )
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f7f3ee;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f3ee;padding:32px 12px;">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border:1px solid #ece4d8;">
<tr><td style="padding:38px 40px 10px;text-align:center;border-bottom:1px solid #ece4d8;">{logo}
<div style="font-family:Arial,sans-serif;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:#b08d57;margin-top:14px;">Bridal Appointments</div>
</td></tr>
<tr><td style="padding:36px 40px 8px;">
<h1 style="margin:0 0 22px;font-family:Georgia,serif;font-weight:normal;font-size:26px;color:#2a2521;">{heading}</h1>
{body_html}{cta_html}
</td></tr>
<tr><td style="padding:22px 40px 34px;border-top:1px solid #ece4d8;text-align:center;">
<p style="margin:0;font-family:Georgia,serif;font-size:15px;color:#b08d57;">With love, Wife To Be</p>
<p style="margin:10px 0 0;font-family:Arial,sans-serif;font-size:10px;letter-spacing:1px;color:#b3aa9c;">Warrington &amp; Runcorn Boutiques</p>
</td></tr>
</table></td></tr></table></body></html>"""


def _smtp_send(cfg: dict, to: str, subject: str, body: str, html: Optional[str] = None) -> bool:
    if not cfg or not cfg.get("smtp_host") or not cfg.get("from_addr"):
        logger.info("Email skipped (SMTP not configured): %s -> %s", subject, to)
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{cfg.get('from_name') or 'Wife To Be'} <{cfg['from_addr']}>"
        msg["To"] = to
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
            if _LOGO_BYTES:
                msg.get_payload()[1].add_related(_LOGO_BYTES, "image", "png", cid="wtblogo")
        with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587)), timeout=15) as server:
            server.starttls()
            if cfg.get("smtp_user"):
                server.login(cfg["smtp_user"], cfg.get("smtp_password", ""))
            server.send_message(msg)
        return True
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return False


def cfg_from_user(u: dict) -> Optional[dict]:
    if u and u.get("smtp_host"):
        return {
            "smtp_host": u.get("smtp_host"),
            "smtp_port": u.get("smtp_port", 587),
            "smtp_user": u.get("smtp_user"),
            "smtp_password": u.get("smtp_password"),
            "from_addr": u.get("sender_email") or u.get("email"),
            "from_name": u.get("sender_name") or "Wife To Be",
        }
    return None


def cfg_from_settings(s: dict) -> Optional[dict]:
    if s and s.get("smtp_host") and s.get("business_email"):
        return {
            "smtp_host": s.get("smtp_host"),
            "smtp_port": s.get("smtp_port", 587),
            "smtp_user": s.get("smtp_user"),
            "smtp_password": s.get("smtp_password"),
            "from_addr": s.get("business_email"),
            "from_name": s.get("from_name") or "Wife To Be",
        }
    return None


async def resolve_cfg(preferred_user: Optional[dict] = None) -> Optional[dict]:
    """Pick an SMTP config: the acting admin's own -> global business -> any admin with SMTP set."""
    if preferred_user:
        c = cfg_from_user(preferred_user)
        if c:
            return c
    c = cfg_from_settings(await get_settings())
    if c:
        return c
    u = await db.users.find_one({"smtp_host": {"$nin": [None, ""]}})
    return cfg_from_user(u) if u else None


# ------------------------------------------------------------------ ICS calendar helpers
def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _ics_dt(date_str: str, time_str: str) -> str:
    return date_str.replace("-", "") + "T" + time_str.replace(":", "") + "00"


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def booking_to_vevent(b: dict) -> str:
    start = _ics_dt(b["date"], b["start_time"])
    end = _ics_dt(b["date"], _to_hhmm(_to_min(b["start_time"]) + int(b.get("duration", 60))))
    loc = _ics_escape(b.get("shop_address") or b.get("shop_name", ""))
    summ = _ics_escape(f"{b.get('appointment_type_name', 'Appointment')} — {b.get('shop_name', '')}")
    desc = _ics_escape(f"Reference {b.get('reference', '')}. {b.get('customer_name', '')}.")
    return "\r\n".join([
        "BEGIN:VEVENT", f"UID:{b.get('reference', uuid.uuid4().hex)}@wifetobe",
        f"DTSTAMP:{start}", f"DTSTART:{start}", f"DTEND:{end}",
        f"SUMMARY:{summ}", f"LOCATION:{loc}", f"DESCRIPTION:{desc}", "END:VEVENT",
    ])


def wrap_ics(events: List[str]) -> str:
    head = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Wife To Be//Appointments//EN\r\nCALSCALE:GREGORIAN\r\n"
    return head + ("\r\n".join(events) + "\r\n" if events else "") + "END:VCALENDAR\r\n"


# ------------------------------------------------------------------ models
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class Verify2FAIn(BaseModel):
    mfa_token: str
    code: str


class ChangePwIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


class Enable2FAIn(BaseModel):
    code: str


class AdminCreateIn(BaseModel):
    name: str
    email: EmailStr
    password: str = Field(min_length=6)


class AdminUpdateIn(BaseModel):
    name: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


class AppointmentTypeIn(BaseModel):
    name: str
    duration: int  # 30/60/90/120
    description: Optional[str] = ""
    active: bool = True


class AvailabilityIn(BaseModel):
    hours: dict  # {"0": {closed, open, close}, ...}
    slot_step: int = 30
    capacity: int = 1     # concurrent appointments per slot
    buffer: int = 0       # minutes gap between appointments


class BlockedDateIn(BaseModel):
    date: str  # YYYY-MM-DD
    reason: Optional[str] = ""
    start_time: Optional[str] = None  # HH:MM -> partial-day block; None = whole day
    end_time: Optional[str] = None


class BookingIn(BaseModel):
    shop_id: str
    appointment_type_id: str
    date: str  # YYYY-MM-DD
    start_time: str  # HH:MM
    customer_name: str
    customer_email: EmailStr
    customer_phone: str
    notes: Optional[str] = ""
    answers: Optional[List[dict]] = None  # [{label, value}]


class BookingUpdateIn(BaseModel):
    status: Optional[str] = None
    date: Optional[str] = None
    start_time: Optional[str] = None
    admin_notes: Optional[str] = None
    payment_status: Optional[str] = None


class ShopUpdateIn(BaseModel):
    blurb: Optional[str] = None
    photo_url: Optional[str] = None
    what_to_expect: Optional[str] = None
    hours_text: Optional[str] = None
    deposit_amount: Optional[float] = None
    deposit_required: Optional[bool] = None


class QuestionsIn(BaseModel):
    questions: List[dict]  # [{id?, label, type, options[], required}]


class WaitlistIn(BaseModel):
    shop_id: str
    appointment_type_id: Optional[str] = None
    date: Optional[str] = None
    customer_name: str
    customer_email: EmailStr
    customer_phone: str
    notes: Optional[str] = ""


class WaitlistUpdateIn(BaseModel):
    status: str  # waiting / contacted


class SettingsIn(BaseModel):
    business_email: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    from_name: Optional[str] = None
    public_url: Optional[str] = None
    notify_customer_on_booking: Optional[bool] = None
    notify_shop_on_booking: Optional[bool] = None
    notify_on_confirm: Optional[bool] = None
    notify_reminder: Optional[bool] = None
    payment_method: Optional[str] = None
    paypal_me_url: Optional[str] = None
    payment_currency: Optional[str] = None


# ------------------------------------------------------------------ auth routes
@api.post("/auth/login")
async def login(body: LoginIn):
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.get("active", True):
        raise HTTPException(status_code=403, detail="This account has been disabled")
    if user.get("totp_enabled"):
        mfa_token = create_token(str(user["_id"]), ttype="mfa", minutes=5)
        return {"mfa_required": True, "mfa_token": mfa_token}
    token = create_token(str(user["_id"]))
    return {"access_token": token, "user": clean(user)}


@api.post("/auth/2fa/verify")
async def verify_2fa(body: Verify2FAIn):
    try:
        payload = decode_token(body.mfa_token)
        if payload.get("type") != "mfa":
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="2FA session expired, please log in again")
    user = await db.users.find_one({"_id": oid(payload["sub"])})
    if not user or not user.get("totp_secret"):
        raise HTTPException(status_code=400, detail="2FA not configured")
    if not pyotp.TOTP(user["totp_secret"]).verify(body.code.strip(), valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid authentication code")
    token = create_token(str(user["_id"]))
    return {"access_token": token, "user": clean(user)}


@api.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return clean(user)


class ProfileIn(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None


@api.post("/auth/update-profile")
async def update_profile(body: ProfileIn, user: dict = Depends(get_current_user)):
    update = {}
    if body.name:
        update["name"] = body.name.strip()
    if body.email:
        email = body.email.lower().strip()
        if email != user["email"]:
            existing = await db.users.find_one({"email": email})
            if existing:
                raise HTTPException(status_code=400, detail="That email is already in use")
            update["email"] = email
    if update:
        await db.users.update_one({"_id": user["_id"]}, {"$set": update})
    doc = await db.users.find_one({"_id": user["_id"]})
    return clean(doc)


class MyEmailSettingsIn(BaseModel):
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    sender_email: Optional[EmailStr] = None
    sender_name: Optional[str] = None


class TestEmailIn(BaseModel):
    to: EmailStr


@api.get("/auth/my-email-settings")
async def get_my_email_settings(user: dict = Depends(get_current_user)):
    return {
        "smtp_host": user.get("smtp_host", ""),
        "smtp_port": user.get("smtp_port", 587),
        "smtp_user": user.get("smtp_user", ""),
        "smtp_password": "********" if user.get("smtp_password") else "",
        "sender_email": user.get("sender_email", "") or user.get("email", ""),
        "sender_name": user.get("sender_name", "") or "Wife To Be",
    }


@api.put("/auth/my-email-settings")
async def put_my_email_settings(body: MyEmailSettingsIn, user: dict = Depends(get_current_user)):
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if update.get("smtp_password") == "********":
        update.pop("smtp_password")
    if "sender_email" in update and update["sender_email"]:
        update["sender_email"] = update["sender_email"].lower().strip()
    if update:
        await db.users.update_one({"_id": user["_id"]}, {"$set": update})
    return {"ok": True}


@api.post("/auth/my-email-settings/test")
async def test_my_email_settings(body: TestEmailIn, user: dict = Depends(get_current_user)):
    u = await db.users.find_one({"_id": user["_id"]})
    cfg = cfg_from_user(u)
    if not cfg:
        raise HTTPException(status_code=400, detail="Please save your SMTP host and details first")
    ok = _smtp_send(cfg, body.to, "Wife To Be — test email",
                    f"This is a test email sent from your Wife To Be account ({cfg['from_addr']}).\n\n"
                    f"If you've received this, your outgoing email is configured correctly.",
                    html=render_email(
                        "Your email is working",
                        [f"This is a test email sent from your Wife To Be account (<strong>{cfg['from_addr']}</strong>).",
                         "If you can see this beautifully formatted message, your outgoing email is configured correctly and ready to send booking notifications."]))
    if not ok:
        raise HTTPException(status_code=400, detail="Could not send — please check your SMTP host, port, username and password")
    return {"ok": True}


@api.post("/auth/change-password")
async def change_password(body: ChangePwIn, user: dict = Depends(get_current_user)):
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": hash_password(body.new_password)}})
    return {"ok": True}


@api.post("/auth/2fa/setup")
async def setup_2fa(user: dict = Depends(get_current_user)):
    secret = pyotp.random_base32()
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"totp_secret": secret}})
    uri = pyotp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name="Wife To Be Appointments")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_data = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return {"secret": secret, "qr": qr_data, "otpauth_uri": uri}


@api.post("/auth/2fa/enable")
async def enable_2fa(body: Enable2FAIn, user: dict = Depends(get_current_user)):
    if not user.get("totp_secret"):
        raise HTTPException(status_code=400, detail="Run 2FA setup first")
    if not pyotp.TOTP(user["totp_secret"]).verify(body.code.strip(), valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code, try again")
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"totp_enabled": True}})
    return {"ok": True}


@api.post("/auth/2fa/disable")
async def disable_2fa(user: dict = Depends(get_current_user)):
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"totp_enabled": False, "totp_secret": None}})
    return {"ok": True}


# ------------------------------------------------------------------ admin management (superadmin)
@api.get("/admins")
async def list_admins(user: dict = Depends(require_superadmin)):
    docs = await db.users.find().sort("created_at", 1).to_list(50)
    return [clean(d) for d in docs]


@api.post("/admins")
async def create_admin(body: AdminCreateIn, user: dict = Depends(require_superadmin)):
    email = body.email.lower().strip()
    count = await db.users.count_documents({"role": "admin"})
    if count >= MAX_ADMINS:
        raise HTTPException(status_code=400, detail=f"Maximum of {MAX_ADMINS} admins reached")
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="An account with this email already exists")
    doc = {
        "name": body.name.strip(),
        "email": email,
        "password_hash": hash_password(body.password),
        "role": "admin",
        "active": True,
        "totp_enabled": False,
        "totp_secret": None,
        "created_at": now_utc().isoformat(),
    }
    res = await db.users.insert_one(doc)
    doc["_id"] = res.inserted_id
    return clean(doc)


@api.patch("/admins/{admin_id}")
async def update_admin(admin_id: str, body: AdminUpdateIn, user: dict = Depends(require_superadmin)):
    target = await db.users.find_one({"_id": oid(admin_id)})
    if not target:
        raise HTTPException(status_code=404, detail="Admin not found")
    if target.get("role") == "superadmin":
        raise HTTPException(status_code=400, detail="Cannot modify the superadmin here")
    update = {}
    if body.name is not None:
        update["name"] = body.name.strip()
    if body.active is not None:
        update["active"] = body.active
    if body.password:
        update["password_hash"] = hash_password(body.password)
    if update:
        await db.users.update_one({"_id": target["_id"]}, {"$set": update})
    doc = await db.users.find_one({"_id": target["_id"]})
    return clean(doc)


@api.delete("/admins/{admin_id}")
async def delete_admin(admin_id: str, user: dict = Depends(require_superadmin)):
    target = await db.users.find_one({"_id": oid(admin_id)})
    if not target:
        raise HTTPException(status_code=404, detail="Admin not found")
    if target.get("role") == "superadmin":
        raise HTTPException(status_code=400, detail="Cannot remove the superadmin")
    await db.users.delete_one({"_id": target["_id"]})
    return {"ok": True}


# ------------------------------------------------------------------ shops (public + admin)
@api.get("/shops")
async def list_shops():
    docs = await db.shops.find().sort("order", 1).to_list(10)
    return [clean(d) for d in docs]


@api.get("/shops/{shop_id}")
async def get_shop(shop_id: str):
    doc = await db.shops.find_one({"_id": oid(shop_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Shop not found")
    return clean(doc)


@api.patch("/shops/{shop_id}")
async def update_shop(shop_id: str, body: ShopUpdateIn, user: dict = Depends(get_current_user)):
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if update:
        await db.shops.update_one({"_id": oid(shop_id)}, {"$set": update})
    doc = await db.shops.find_one({"_id": oid(shop_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Shop not found")
    return clean(doc)


@api.put("/shops/{shop_id}/questions")
async def set_questions(shop_id: str, body: QuestionsIn, user: dict = Depends(get_current_user)):
    questions = []
    for q in body.questions:
        questions.append({
            "id": q.get("id") or uuid.uuid4().hex[:8],
            "label": (q.get("label") or "").strip(),
            "type": q.get("type") or "text",
            "options": q.get("options") or [],
            "required": bool(q.get("required")),
        })
    await db.shops.update_one({"_id": oid(shop_id)}, {"$set": {"questions": questions}})
    return {"questions": questions}


# ------------------------------------------------------------------ appointment types
@api.get("/shops/{shop_id}/appointment-types")
async def list_types(shop_id: str, all: bool = False):
    query = {"shop_id": shop_id}
    if not all:
        query["active"] = True
    docs = await db.appointment_types.find(query).sort("duration", 1).to_list(50)
    return [clean(d) for d in docs]


@api.post("/shops/{shop_id}/appointment-types")
async def create_type(shop_id: str, body: AppointmentTypeIn, user: dict = Depends(get_current_user)):
    if not (5 <= body.duration <= 600):
        raise HTTPException(status_code=400, detail="Duration must be between 5 and 600 minutes")
    doc = body.model_dump()
    doc["shop_id"] = shop_id
    res = await db.appointment_types.insert_one(doc)
    doc["_id"] = res.inserted_id
    return clean(doc)


@api.patch("/appointment-types/{type_id}")
async def update_type(type_id: str, body: AppointmentTypeIn, user: dict = Depends(get_current_user)):
    if not (5 <= body.duration <= 600):
        raise HTTPException(status_code=400, detail="Duration must be between 5 and 600 minutes")
    await db.appointment_types.update_one({"_id": oid(type_id)}, {"$set": body.model_dump()})
    doc = await db.appointment_types.find_one({"_id": oid(type_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return clean(doc)


@api.delete("/appointment-types/{type_id}")
async def delete_type(type_id: str, user: dict = Depends(get_current_user)):
    await db.appointment_types.delete_one({"_id": oid(type_id)})
    return {"ok": True}


# ------------------------------------------------------------------ availability
@api.get("/shops/{shop_id}/availability")
async def get_availability(shop_id: str):
    doc = await db.availability.find_one({"shop_id": shop_id})
    if not doc:
        return {"shop_id": shop_id, "hours": {}, "slot_step": 30, "capacity": 1, "buffer": 0}
    doc.pop("_id", None)
    doc.setdefault("capacity", 1)
    doc.setdefault("buffer", 0)
    return doc


@api.put("/shops/{shop_id}/availability")
async def set_availability(shop_id: str, body: AvailabilityIn, user: dict = Depends(get_current_user)):
    await db.availability.update_one(
        {"shop_id": shop_id},
        {"$set": {"shop_id": shop_id, "hours": body.hours, "slot_step": body.slot_step,
                  "capacity": max(1, body.capacity), "buffer": max(0, body.buffer)}},
        upsert=True,
    )
    return {"ok": True}


@api.get("/shops/{shop_id}/blocked-dates")
async def get_blocked(shop_id: str):
    docs = await db.blocked_dates.find({"shop_id": shop_id}).sort("date", 1).to_list(365)
    return [clean(d) for d in docs]


@api.post("/shops/{shop_id}/blocked-dates")
async def add_blocked(shop_id: str, body: BlockedDateIn, user: dict = Depends(get_current_user)):
    doc = {"shop_id": shop_id, "date": body.date, "reason": body.reason,
           "start_time": body.start_time or None, "end_time": body.end_time or None}
    res = await db.blocked_dates.insert_one(doc)
    doc["_id"] = res.inserted_id
    return clean(doc)


@api.delete("/blocked-dates/{block_id}")
async def del_blocked(block_id: str, user: dict = Depends(get_current_user)):
    await db.blocked_dates.delete_one({"_id": oid(block_id)})
    return {"ok": True}


# ------------------------------------------------------------------ slot computation
async def _compute_slots(shop_id: str, date: str, duration: int, exclude_ref: Optional[str] = None):
    if not (5 <= duration <= 600):
        raise HTTPException(status_code=400, detail="Invalid duration")
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")
    if d < datetime.now().date():
        return {"slots": []}
    blocks = await db.blocked_dates.find({"shop_id": shop_id, "date": date}).to_list(50)
    # whole-day block (no time range) => closed
    if any(not b.get("start_time") or not b.get("end_time") for b in blocks):
        return {"slots": [], "reason": "closed"}
    timed = [(_to_min(b["start_time"]), _to_min(b["end_time"])) for b in blocks]
    avail = await db.availability.find_one({"shop_id": shop_id})
    if not avail:
        return {"slots": []}
    day = avail.get("hours", {}).get(str(d.weekday()))  # Mon=0
    if not day or day.get("closed"):
        return {"slots": [], "reason": "closed"}
    step = avail.get("slot_step", 30)
    capacity = max(1, avail.get("capacity", 1))
    buffer = max(0, avail.get("buffer", 0))
    open_m, close_m = _to_min(day["open"]), _to_min(day["close"])
    q = {"shop_id": shop_id, "date": date, "status": {"$ne": "cancelled"}}
    if exclude_ref:
        q["reference"] = {"$ne": exclude_ref}
    existing = await db.bookings.find(q).to_list(300)
    # each existing occupies [start - buffer, start + duration + buffer]
    busy = [(_to_min(b["start_time"]) - buffer, _to_min(b["start_time"]) + b["duration"] + buffer) for b in existing]
    slots = []
    t = open_m
    while t + duration <= close_m:
        s_start, s_end = t, t + duration
        blocked = any(s_start < be and s_end > bs for bs, be in timed)
        overlaps = sum(1 for bs, be in busy if s_start < be and s_end > bs)
        if not blocked and overlaps < capacity:
            slots.append(_to_hhmm(t))
        t += step
    return {"slots": slots}


@api.get("/public/slots")
async def public_slots(shop_id: str, date: str, duration: int):
    return await _compute_slots(shop_id, date, duration)


# ------------------------------------------------------------------ bookings
@api.post("/public/bookings")
async def create_booking(body: BookingIn):
    shop = await db.shops.find_one({"_id": oid(body.shop_id)})
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
    atype = await db.appointment_types.find_one({"_id": oid(body.appointment_type_id)})
    if not atype:
        raise HTTPException(status_code=404, detail="Appointment type not found")
    duration = atype["duration"]
    avail = await _compute_slots(body.shop_id, body.date, duration)
    if body.start_time not in avail.get("slots", []):
        raise HTTPException(status_code=409, detail="That time slot is no longer available")
    ref = "WTB-" + base64.b32encode(os.urandom(5)).decode().rstrip("=")[:8]
    settings = await get_settings()
    method = settings.get("payment_method", "off")
    deposit = float(shop.get("deposit_amount") or 0)
    if method == "off" or deposit <= 0:
        pay_status = "not_required"
    elif method == "in_person":
        pay_status = "pay_in_person"
    else:  # paypal_me / paypal -> awaiting payment
        pay_status = "pending"
    doc = {
        "reference": ref,
        "shop_id": body.shop_id,
        "shop_name": shop["name"],
        "shop_address": shop.get("address", ""),
        "appointment_type_id": body.appointment_type_id,
        "appointment_type_name": atype["name"],
        "duration": duration,
        "date": body.date,
        "start_time": body.start_time,
        "customer_name": body.customer_name.strip(),
        "customer_email": body.customer_email.lower().strip(),
        "customer_phone": body.customer_phone.strip(),
        "notes": body.notes,
        "answers": body.answers or [],
        "admin_notes": "",
        "status": "pending",
        "reminder_sent": False,
        "deposit_amount": deposit if pay_status != "not_required" else 0,
        "deposit_required": bool(shop.get("deposit_required")),
        "payment_status": pay_status,
        "payment_method_used": "" if pay_status == "not_required" else method,
        "payment_ref": "",
        "created_at": now_utc().isoformat(),
    }
    res = await db.bookings.insert_one(doc)
    doc["_id"] = res.inserted_id
    cfg = await resolve_cfg()
    when = f"{body.date} at {body.start_time}"
    murl = manage_url(settings, ref)
    if settings.get("notify_customer_on_booking"):
        _smtp_send(cfg, doc["customer_email"], "Your Wife To Be appointment request",
                   f"Dear {doc['customer_name']},\n\nWe have received your request for a {atype['name']} at our {shop['name']} boutique on {when}. "
                   f"Your reference is {ref}. We will confirm your appointment shortly.{manage_link(settings, ref)}\n\nWith love,\nWife To Be",
                   html=render_email(
                       f"Thank you, {doc['customer_name'].split(' ')[0]}",
                       [f"We've received your request for a <strong>{atype['name']}</strong> at our <strong>{shop['name']}</strong> boutique on <strong>{when}</strong>.",
                        f"Your booking reference is <strong>{ref}</strong>. We'll be in touch very shortly to confirm your appointment.",
                        "We can't wait to welcome you."],
                       cta={"url": murl, "label": "View My Appointment"} if murl else None))
    shop_to = settings.get("business_email") or (cfg or {}).get("from_addr")
    if settings.get("notify_shop_on_booking") and shop_to:
        _smtp_send(cfg, shop_to, f"New booking request — {shop['name']}",
                   f"{doc['customer_name']} ({doc['customer_email']}, {doc['customer_phone']}) requested a {atype['name']} on {when}. Ref {ref}.",
                   html=render_email(
                       "New Booking Request",
                       [f"<strong>{doc['customer_name']}</strong> has requested a <strong>{atype['name']}</strong> at <strong>{shop['name']}</strong> on <strong>{when}</strong>.",
                        f"Email: {doc['customer_email']}<br>Phone: {doc['customer_phone']}<br>Reference: {ref}"]))
    return clean(doc)


class PublicReschedIn(BaseModel):
    date: str
    start_time: str


@api.post("/public/bookings/{reference}/reschedule")
async def public_reschedule(reference: str, body: PublicReschedIn):
    b = await db.bookings.find_one({"reference": reference})
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    if b["status"] in ("cancelled", "completed"):
        raise HTTPException(status_code=400, detail="This booking can no longer be changed")
    avail = await _compute_slots(b["shop_id"], body.date, b["duration"], exclude_ref=reference)
    if body.start_time not in avail.get("slots", []):
        raise HTTPException(status_code=409, detail="That time slot is no longer available")
    await db.bookings.update_one({"_id": b["_id"]},
                                 {"$set": {"date": body.date, "start_time": body.start_time, "status": "pending", "reminder_sent": False}})
    doc = await db.bookings.find_one({"_id": b["_id"]})
    return clean(doc)


@api.post("/public/bookings/{reference}/cancel")
async def public_cancel(reference: str):
    b = await db.bookings.find_one({"reference": reference})
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    if b["status"] in ("cancelled", "completed"):
        raise HTTPException(status_code=400, detail="This booking can no longer be changed")
    await db.bookings.update_one({"_id": b["_id"]}, {"$set": {"status": "cancelled"}})
    return {"ok": True}


@api.get("/public/bookings/{reference}/calendar.ics")
async def booking_ics(reference: str):
    b = await db.bookings.find_one({"reference": reference})
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    ics = wrap_ics([booking_to_vevent(b)])
    return Response(content=ics, media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="{reference}.ics"'})


@api.get("/public/bookings/{reference}")
async def get_booking_by_ref(reference: str):
    doc = await db.bookings.find_one({"reference": reference})
    if not doc:
        raise HTTPException(status_code=404, detail="Booking not found")
    return clean(doc)


# ------------------------------------------------------------------ payments
def _paypal_env():
    return {
        "client_id": os.environ.get("PAYPAL_CLIENT_ID", ""),
        "secret": os.environ.get("PAYPAL_SECRET", ""),
        "mode": os.environ.get("PAYPAL_MODE", "sandbox"),
    }


def _paypal_base(mode: str) -> str:
    return "https://api-m.paypal.com" if mode == "live" else "https://api-m.sandbox.paypal.com"


@api.get("/payments/config")
async def payments_config():
    s = await get_settings()
    env = _paypal_env()
    return {
        "method": s.get("payment_method", "off"),
        "paypal_me_url": (s.get("paypal_me_url") or "").rstrip("/"),
        "currency": s.get("payment_currency", "GBP"),
        "paypal_client_id": env["client_id"],
        "paypal_configured": bool(env["client_id"] and env["secret"]),
    }


@api.post("/public/bookings/{reference}/pay-in-person")
async def mark_pay_in_person(reference: str):
    b = await db.bookings.find_one({"reference": reference})
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    await db.bookings.update_one({"_id": b["_id"]},
                                 {"$set": {"payment_status": "pay_in_person", "payment_method_used": "in_person"}})
    doc = await db.bookings.find_one({"_id": b["_id"]})
    return clean(doc)


async def _paypal_token(env: dict) -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{_paypal_base(env['mode'])}/v1/oauth2/token",
                         auth=(env["client_id"], env["secret"]),
                         data={"grant_type": "client_credentials"})
        r.raise_for_status()
        return r.json()["access_token"]


@api.post("/public/bookings/{reference}/paypal/create-order")
async def paypal_create_order(reference: str):
    env = _paypal_env()
    if not (env["client_id"] and env["secret"]):
        raise HTTPException(status_code=400, detail="PayPal is not configured")
    b = await db.bookings.find_one({"reference": reference})
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    amount = float(b.get("deposit_amount") or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="No deposit is due for this booking")
    s = await get_settings()
    currency = s.get("payment_currency", "GBP")
    try:
        token = await _paypal_token(env)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{_paypal_base(env['mode'])}/v2/checkout/orders",
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"intent": "CAPTURE", "purchase_units": [{
                                 "reference_id": b["reference"],
                                 "description": f"Deposit — {b['appointment_type_name']} at {b['shop_name']}",
                                 "amount": {"currency_code": currency, "value": f"{amount:.2f}"}}]})
            r.raise_for_status()
            return {"id": r.json()["id"]}
    except httpx.HTTPError as e:
        logger.error("PayPal create-order failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not start PayPal payment")


@api.post("/public/bookings/{reference}/paypal/capture-order")
async def paypal_capture_order(reference: str, order_id: str):
    env = _paypal_env()
    if not (env["client_id"] and env["secret"]):
        raise HTTPException(status_code=400, detail="PayPal is not configured")
    b = await db.bookings.find_one({"reference": reference})
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    try:
        token = await _paypal_token(env)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{_paypal_base(env['mode'])}/v2/checkout/orders/{order_id}/capture",
                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        logger.error("PayPal capture failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not confirm PayPal payment")
    if data.get("status") == "COMPLETED":
        await db.bookings.update_one({"_id": b["_id"]}, {"$set": {
            "payment_status": "paid", "payment_method_used": "paypal",
            "payment_ref": order_id, "paid_at": now_utc().isoformat()}})
    doc = await db.bookings.find_one({"_id": b["_id"]})
    return clean(doc)



def _booking_query(shop_id, status, date_from, date_to):
    query: dict = {}
    if shop_id:
        query["shop_id"] = shop_id
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    return query


@api.get("/bookings")
async def list_bookings(user: dict = Depends(get_current_user), shop_id: Optional[str] = None,
                        status: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None):
    query = _booking_query(shop_id, status, date_from, date_to)
    docs = await db.bookings.find(query).sort([("date", 1), ("start_time", 1)]).to_list(1000)
    return [clean(d) for d in docs]


@api.get("/bookings/export.csv")
async def export_bookings_csv(user: dict = Depends(get_current_user), shop_id: Optional[str] = None,
                              status: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None):
    query = _booking_query(shop_id, status, date_from, date_to)
    docs = await db.bookings.find(query).sort([("date", 1), ("start_time", 1)]).to_list(5000)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Reference", "Date", "Time", "Duration (min)", "Status", "Boutique", "Appointment", "Customer", "Email", "Phone", "Deposit", "Payment", "Notes", "Created"])
    for b in docs:
        w.writerow([b.get("reference", ""), b.get("date", ""), b.get("start_time", ""), b.get("duration", ""),
                    b.get("status", ""), b.get("shop_name", ""), b.get("appointment_type_name", ""),
                    b.get("customer_name", ""), b.get("customer_email", ""), b.get("customer_phone", ""),
                    b.get("deposit_amount", 0), b.get("payment_status", ""),
                    (b.get("notes") or "").replace("\n", " "), b.get("created_at", "")])
    return Response(content=out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="wifetobe-bookings.csv"'})


@api.get("/calendar/{feed_token}.ics")
async def calendar_feed(feed_token: str, shop_id: Optional[str] = None):
    s = await get_settings()
    if not s.get("feed_token") or feed_token != s["feed_token"]:
        raise HTTPException(status_code=404, detail="Calendar feed not found")
    today = datetime.now().date().isoformat()
    q = {"date": {"$gte": today}, "status": {"$ne": "cancelled"}}
    if shop_id:
        q["shop_id"] = shop_id
    docs = await db.bookings.find(q).to_list(2000)
    return Response(content=wrap_ics([booking_to_vevent(b) for b in docs]), media_type="text/calendar")


@api.patch("/bookings/{booking_id}")
async def update_booking(booking_id: str, body: BookingUpdateIn, user: dict = Depends(get_current_user)):
    booking = await db.bookings.find_one({"_id": oid(booking_id)})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    update = {}
    if body.status:
        if body.status not in ("pending", "confirmed", "cancelled", "completed", "no_show"):
            raise HTTPException(status_code=400, detail="Invalid status")
        update["status"] = body.status
    if body.date:
        update["date"] = body.date
    if body.start_time:
        update["start_time"] = body.start_time
    if body.admin_notes is not None:
        update["admin_notes"] = body.admin_notes
    if body.payment_status is not None:
        if body.payment_status not in ("not_required", "pending", "paid", "pay_in_person"):
            raise HTTPException(status_code=400, detail="Invalid payment status")
        update["payment_status"] = body.payment_status
        if body.payment_status == "paid":
            update["paid_at"] = now_utc().isoformat()
            if not booking.get("payment_method_used"):
                update["payment_method_used"] = "manual"
    if update:
        await db.bookings.update_one({"_id": booking["_id"]}, {"$set": update})
    doc = await db.bookings.find_one({"_id": booking["_id"]})
    if body.status == "confirmed":
        settings = await get_settings()
        if settings.get("notify_on_confirm"):
            cfg = await resolve_cfg(user)
            murl = manage_url(settings, doc["reference"])
            _smtp_send(cfg, doc["customer_email"], "Your Wife To Be appointment is confirmed",
                       f"Dear {doc['customer_name']},\n\nYour {doc['appointment_type_name']} at our {doc['shop_name']} boutique on "
                       f"{doc['date']} at {doc['start_time']} is now confirmed. Reference {doc['reference']}.{manage_link(settings, doc['reference'])}\n\nWe can't wait to see you.\nWife To Be",
                       html=render_email(
                           f"You're confirmed, {doc['customer_name'].split(' ')[0]}",
                           [f"Your <strong>{doc['appointment_type_name']}</strong> at our <strong>{doc['shop_name']}</strong> boutique is now confirmed for:",
                            f"<strong>{doc['date']} at {doc['start_time']}</strong>",
                            f"Reference: <strong>{doc['reference']}</strong>. We can't wait to see you."],
                           cta={"url": murl, "label": "View My Appointment"} if murl else None))
    return clean(doc)


class FollowUpIn(BaseModel):
    date: str
    start_time: str
    appointment_type_id: Optional[str] = None
    label: Optional[str] = ""  # e.g. "2nd fitting", "Final fitting"


@api.post("/bookings/{booking_id}/follow-up")
async def create_follow_up(booking_id: str, body: FollowUpIn, user: dict = Depends(get_current_user)):
    parent = await db.bookings.find_one({"_id": oid(booking_id)})
    if not parent:
        raise HTTPException(status_code=404, detail="Original booking not found")
    type_id = body.appointment_type_id or parent["appointment_type_id"]
    atype = await db.appointment_types.find_one({"_id": oid(type_id)})
    if not atype:
        raise HTTPException(status_code=404, detail="Appointment type not found")
    duration = atype["duration"]
    avail = await _compute_slots(parent["shop_id"], body.date, duration)
    if body.start_time not in avail.get("slots", []):
        raise HTTPException(status_code=409, detail="That time slot is no longer available")
    series_id = parent.get("series_id") or str(parent["_id"])
    if not parent.get("series_id"):
        await db.bookings.update_one({"_id": parent["_id"]}, {"$set": {"series_id": series_id}})
    ref = "WTB-" + base64.b32encode(os.urandom(5)).decode().rstrip("=")[:8]
    doc = {
        "reference": ref, "shop_id": parent["shop_id"], "shop_name": parent["shop_name"],
        "shop_address": parent.get("shop_address", ""),
        "appointment_type_id": type_id, "appointment_type_name": atype["name"], "duration": duration,
        "date": body.date, "start_time": body.start_time,
        "customer_name": parent["customer_name"], "customer_email": parent["customer_email"],
        "customer_phone": parent["customer_phone"],
        "notes": (body.label or "Follow-up appointment"), "answers": [], "admin_notes": "",
        "status": "confirmed", "reminder_sent": False, "series_id": series_id,
        "created_at": now_utc().isoformat(),
    }
    res = await db.bookings.insert_one(doc)
    doc["_id"] = res.inserted_id
    return clean(doc)


@api.get("/bookings/{booking_id}/series")
async def booking_series(booking_id: str, user: dict = Depends(get_current_user)):
    b = await db.bookings.find_one({"_id": oid(booking_id)})
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    series_id = b.get("series_id") or str(b["_id"])
    docs = await db.bookings.find({"series_id": series_id}).sort([("date", 1), ("start_time", 1)]).to_list(100)
    return [clean(d) for d in docs]


# ------------------------------------------------------------------ analytics
@api.get("/analytics")
async def analytics(user: dict = Depends(get_current_user), shop_id: Optional[str] = None):
    q: dict = {}
    if shop_id:
        q["shop_id"] = shop_id
    docs = await db.bookings.find(q).to_list(20000)
    today = datetime.now().date().isoformat()
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    by_weekday = {w: 0 for w in weekday_names}
    by_hour: dict = {}
    by_shop: dict = {}
    by_source: dict = {}
    completed = no_show = 0
    active = 0
    for b in docs:
        status = b.get("status")
        if status == "cancelled":
            continue
        active += 1
        try:
            d = datetime.strptime(b["date"], "%Y-%m-%d").date()
            by_weekday[weekday_names[d.weekday()]] += 1
        except Exception:
            pass
        hh = (b.get("start_time") or "")[:2]
        if hh:
            by_hour[hh] = by_hour.get(hh, 0) + 1
        sn = b.get("shop_name", "Unknown")
        by_shop[sn] = by_shop.get(sn, 0) + 1
        for a in (b.get("answers") or []):
            if a.get("label") == "How did you hear about us?" and a.get("value"):
                by_source[a["value"]] = by_source.get(a["value"], 0) + 1
        if status == "completed":
            completed += 1
        elif status == "no_show":
            no_show += 1
    total_attended = completed + no_show
    no_show_rate = round((no_show / total_attended) * 100) if total_attended else 0
    hours_sorted = [{"hour": f"{h}:00", "count": c} for h, c in sorted(by_hour.items())]
    return {
        "total": active,
        "by_weekday": [{"day": w, "count": by_weekday[w]} for w in weekday_names],
        "by_hour": hours_sorted,
        "by_shop": [{"shop": k, "count": v} for k, v in sorted(by_shop.items(), key=lambda x: -x[1])],
        "by_source": [{"source": k, "count": v} for k, v in sorted(by_source.items(), key=lambda x: -x[1])],
        "completed": completed, "no_show": no_show, "no_show_rate": no_show_rate,
    }


# ------------------------------------------------------------------ customers
@api.get("/customers")
async def list_customers(user: dict = Depends(get_current_user), q: Optional[str] = None):
    docs = await db.bookings.find({}).sort([("date", -1)]).to_list(20000)
    people: dict = {}
    for b in docs:
        email = (b.get("customer_email") or "").lower()
        if not email:
            continue
        p = people.setdefault(email, {"email": email, "name": b.get("customer_name", ""),
                                      "phone": b.get("customer_phone", ""), "total": 0,
                                      "last_date": "", "last_shop": ""})
        p["total"] += 1
        if b.get("date", "") > p["last_date"]:
            p["last_date"] = b.get("date", "")
            p["last_shop"] = b.get("shop_name", "")
            p["name"] = b.get("customer_name", "") or p["name"]
            p["phone"] = b.get("customer_phone", "") or p["phone"]
    rows = list(people.values())
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["name"].lower() or ql in r["email"] or ql in (r["phone"] or "")]
    rows.sort(key=lambda r: r["last_date"], reverse=True)
    return rows


@api.get("/customers/{email}")
async def customer_detail(email: str, user: dict = Depends(get_current_user)):
    em = email.lower().strip()
    docs = await db.bookings.find({"customer_email": em}).sort([("date", -1), ("start_time", -1)]).to_list(500)
    if not docs:
        raise HTTPException(status_code=404, detail="No bookings found for this customer")
    latest = docs[0]
    return {
        "email": em,
        "name": latest.get("customer_name", ""),
        "phone": latest.get("customer_phone", ""),
        "total": len(docs),
        "bookings": [clean(d) for d in docs],
    }



# ------------------------------------------------------------------ waitlist
@api.post("/public/waitlist")
async def add_waitlist(body: WaitlistIn):
    shop = await db.shops.find_one({"_id": oid(body.shop_id)})
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
    atype_name = ""
    if body.appointment_type_id:
        at = await db.appointment_types.find_one({"_id": oid(body.appointment_type_id)})
        atype_name = at["name"] if at else ""
    doc = {
        "shop_id": body.shop_id, "shop_name": shop["name"],
        "appointment_type_id": body.appointment_type_id, "appointment_type_name": atype_name,
        "date": body.date or "", "customer_name": body.customer_name.strip(),
        "customer_email": body.customer_email.lower().strip(), "customer_phone": body.customer_phone.strip(),
        "notes": body.notes, "status": "waiting", "created_at": now_utc().isoformat(),
    }
    res = await db.waitlist.insert_one(doc)
    doc["_id"] = res.inserted_id
    return clean(doc)


@api.get("/waitlist")
async def list_waitlist(user: dict = Depends(get_current_user), shop_id: Optional[str] = None):
    q = {}
    if shop_id:
        q["shop_id"] = shop_id
    docs = await db.waitlist.find(q).sort("created_at", -1).to_list(500)
    return [clean(d) for d in docs]


@api.patch("/waitlist/{item_id}")
async def update_waitlist(item_id: str, body: WaitlistUpdateIn, user: dict = Depends(get_current_user)):
    await db.waitlist.update_one({"_id": oid(item_id)}, {"$set": {"status": body.status}})
    doc = await db.waitlist.find_one({"_id": oid(item_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return clean(doc)


@api.delete("/waitlist/{item_id}")
async def delete_waitlist(item_id: str, user: dict = Depends(get_current_user)):
    await db.waitlist.delete_one({"_id": oid(item_id)})
    return {"ok": True}


@api.get("/dashboard/stats")
async def dashboard_stats(user: dict = Depends(get_current_user)):
    today = datetime.now().date().isoformat()
    total = await db.bookings.count_documents({})
    pending = await db.bookings.count_documents({"status": "pending"})
    confirmed = await db.bookings.count_documents({"status": "confirmed"})
    upcoming = await db.bookings.count_documents({"date": {"$gte": today}, "status": {"$ne": "cancelled"}})
    today_list = await db.bookings.find({"date": today, "status": {"$ne": "cancelled"}}).sort("start_time", 1).to_list(100)
    per_shop = []
    shops = await db.shops.find().sort("order", 1).to_list(10)
    for s in shops:
        c = await db.bookings.count_documents({"shop_id": str(s["_id"]), "date": {"$gte": today}, "status": {"$ne": "cancelled"}})
        per_shop.append({"shop": s["name"], "upcoming": c})
    waitlist = await db.waitlist.count_documents({"status": "waiting"})
    return {
        "total": total, "pending": pending, "confirmed": confirmed, "upcoming": upcoming,
        "waitlist": waitlist, "today": [clean(d) for d in today_list], "per_shop": per_shop,
    }


# ------------------------------------------------------------------ settings
@api.get("/settings")
async def read_settings(user: dict = Depends(require_superadmin)):
    s = await get_settings()
    if not s.get("feed_token"):
        token = secrets.token_urlsafe(16)
        await db.settings.update_one({"_id": "global"}, {"$set": {"feed_token": token}})
        s["feed_token"] = token
    s.pop("_id", None)
    s["smtp_password"] = "********" if s.get("smtp_password") else ""
    return s


@api.put("/settings")
async def write_settings(body: SettingsIn, user: dict = Depends(require_superadmin)):
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if update.get("smtp_password") == "********":
        update.pop("smtp_password")
    await db.settings.update_one({"_id": "global"}, {"$set": update}, upsert=True)
    return {"ok": True}


# ------------------------------------------------------------------ seed + startup
async def seed():
    await db.users.create_index("email", unique=True)
    await db.bookings.create_index([("shop_id", 1), ("date", 1), ("status", 1)])
    await db.bookings.create_index("reference", unique=True)
    await db.blocked_dates.create_index([("shop_id", 1), ("date", 1)])
    await db.appointment_types.create_index("shop_id")
    email = os.environ["SUPERADMIN_EMAIL"].lower()
    existing = await db.users.find_one({"email": email})
    if not existing:
        await db.users.insert_one({
            "name": os.environ.get("SUPERADMIN_NAME", "Superadmin"),
            "email": email,
            "password_hash": hash_password(os.environ["SUPERADMIN_PASSWORD"]),
            "role": "superadmin",
            "active": True,
            "totp_enabled": False,
            "totp_secret": None,
            "created_at": now_utc().isoformat(),
        })
        logger.info("Seeded superadmin %s", email)
    elif not verify_password(os.environ["SUPERADMIN_PASSWORD"], existing["password_hash"]):
        await db.users.update_one({"email": email}, {"$set": {"password_hash": hash_password(os.environ["SUPERADMIN_PASSWORD"])}})

    await get_settings()

    # backfill optional fields on any pre-existing shop documents (safe, additive)
    await db.shops.update_many({"photo_url": {"$exists": False}}, {"$set": {"photo_url": ""}})
    await db.shops.update_many({"questions": {"$exists": False}}, {"$set": {"questions": []}})
    await db.shops.update_many({"hours_text": {"$exists": False}}, {"$set": {"hours_text": ""}})
    await db.shops.update_many({"what_to_expect": {"$exists": False}}, {"$set": {"what_to_expect": ""}})
    await db.availability.update_many({"capacity": {"$exists": False}}, {"$set": {"capacity": 1}})
    await db.availability.update_many({"buffer": {"$exists": False}}, {"$set": {"buffer": 0}})
    await db.shops.update_many({"deposit_amount": {"$exists": False}}, {"$set": {"deposit_amount": 0}})
    await db.shops.update_many({"deposit_required": {"$exists": False}}, {"$set": {"deposit_required": False}})
    await db.bookings.update_many({"payment_status": {"$exists": False}}, {"$set": {"payment_status": "not_required", "deposit_amount": 0, "payment_method_used": "", "payment_ref": ""}})

    # add "How did you hear about us?" question to shops that don't have it yet (additive)
    SOURCE_Q = {
        "id": "source",
        "label": "How did you hear about us?",
        "type": "dropdown",
        "options": ["Instagram", "Facebook", "Google Search", "Friend / Word of Mouth", "Wedding Fair", "Walk-in / Passing", "Other"],
        "required": False,
    }
    async for shop in db.shops.find({}):
        qs = shop.get("questions") or []
        if not any((q.get("id") == "source" or q.get("label") == SOURCE_Q["label"]) for q in qs):
            qs.append(SOURCE_Q)
            await db.shops.update_one({"_id": shop["_id"]}, {"$set": {"questions": qs}})

    if await db.shops.count_documents({}) == 0:
        warr = {
            "name": "Warrington Boutique", "slug": "warrington", "role_label": "Wedding Dresses",
            "address": "3-5 Fennel Street, Warrington, WA1 2PA", "phone": "01925 570093",
            "email": "thegroupuk@yahoo.com", "order": 0,
            "blurb": "Home to over 200 designer wedding gowns — a private, unhurried styling experience.",
            "hours_text": "Tue–Fri 11am–5pm · Sat 10am–5pm",
            "what_to_expect": "", "photo_url": "", "questions": [],
        }
        runc = {
            "name": "Runcorn Boutique", "slug": "runcorn", "role_label": "Suit Hire",
            "address": "136 Greenway Road, Runcorn, WA7 5BS", "phone": "0151 420 0151",
            "email": "thegroupuk@yahoo.com", "order": 1,
            "blurb": "Men's formal wear & suit hire, by appointment only.",
            "hours_text": "By appointment only", "what_to_expect": "", "photo_url": "", "questions": [],
        }
        rw = await db.shops.insert_one(warr)
        rr = await db.shops.insert_one(runc)
        warr_id, runc_id = str(rw.inserted_id), str(rr.inserted_id)
        warr_hours = {
            "0": {"closed": True, "open": "11:00", "close": "17:00"},
            "1": {"closed": False, "open": "11:00", "close": "17:00"},
            "2": {"closed": False, "open": "11:00", "close": "17:00"},
            "3": {"closed": False, "open": "11:00", "close": "17:00"},
            "4": {"closed": False, "open": "11:00", "close": "17:00"},
            "5": {"closed": False, "open": "10:00", "close": "17:00"},
            "6": {"closed": True, "open": "10:00", "close": "17:00"},
        }
        runc_hours = {
            "0": {"closed": True, "open": "10:00", "close": "17:00"},
            "1": {"closed": False, "open": "10:00", "close": "17:00"},
            "2": {"closed": False, "open": "10:00", "close": "17:00"},
            "3": {"closed": False, "open": "10:00", "close": "17:00"},
            "4": {"closed": False, "open": "10:00", "close": "17:00"},
            "5": {"closed": False, "open": "10:00", "close": "16:00"},
            "6": {"closed": True, "open": "10:00", "close": "17:00"},
        }
        await db.availability.insert_one({"shop_id": warr_id, "hours": warr_hours, "slot_step": 30, "capacity": 1, "buffer": 0})
        await db.availability.insert_one({"shop_id": runc_id, "hours": runc_hours, "slot_step": 30, "capacity": 1, "buffer": 0})
        wtypes = [
            {"name": "Bridal Appointment", "duration": 90, "description": "Our signature private styling session.", "active": True},
            {"name": "First Look Consultation", "duration": 60, "description": "Begin your search with expert guidance.", "active": True},
            {"name": "Dress Fitting", "duration": 60, "description": "Fittings & alteration guidance.", "active": True},
            {"name": "Gown Collection", "duration": 30, "description": "Collect your finished gown.", "active": True},
        ]
        rtypes = [
            {"name": "Suit Hire Consultation", "duration": 60, "description": "Choose your formal wear.", "active": True},
            {"name": "Suit Fitting", "duration": 30, "description": "Measurements & fitting.", "active": True},
        ]
        for t in wtypes:
            await db.appointment_types.insert_one({**t, "shop_id": warr_id})
        for t in rtypes:
            await db.appointment_types.insert_one({**t, "shop_id": runc_id})
        logger.info("Seeded shops, availability and appointment types")


async def reminder_loop():
    while True:
        try:
            settings = await get_settings()
            if settings.get("notify_reminder"):
                cfg = await resolve_cfg()
                now = datetime.now()
                lo, hi = now + timedelta(hours=23), now + timedelta(hours=25)
                candidates = await db.bookings.find(
                    {"status": "confirmed", "reminder_sent": {"$ne": True}}
                ).to_list(500)
                for b in candidates:
                    try:
                        dt = datetime.strptime(f"{b['date']} {b['start_time']}", "%Y-%m-%d %H:%M")
                    except Exception:
                        continue
                    if lo <= dt <= hi:
                        murl = manage_url(settings, b["reference"])
                        sent = _smtp_send(cfg, b["customer_email"], "Your Wife To Be appointment is tomorrow",
                                   f"Dear {b['customer_name']},\n\nA gentle reminder of your {b['appointment_type_name']} at our {b['shop_name']} boutique "
                                   f"tomorrow, {b['date']} at {b['start_time']}. Reference {b['reference']}.{manage_link(settings, b['reference'])}\n\nWe look forward to seeing you.\nWife To Be",
                                   html=render_email(
                                       "See you tomorrow",
                                       [f"Dear {b['customer_name'].split(' ')[0]}, a gentle reminder of your <strong>{b['appointment_type_name']}</strong> at our <strong>{b['shop_name']}</strong> boutique tomorrow:",
                                        f"<strong>{b['date']} at {b['start_time']}</strong>",
                                        f"Reference: <strong>{b['reference']}</strong>. We look forward to welcoming you."],
                                       cta={"url": murl, "label": "View My Appointment"} if murl else None))
                        if sent:
                            await db.bookings.update_one({"_id": b["_id"]}, {"$set": {"reminder_sent": True}})
        except Exception as e:
            logger.error("reminder loop error: %s", e)
        await asyncio.sleep(1800)


@app.on_event("startup")
async def startup():
    await seed()
    asyncio.create_task(reminder_loop())


@app.on_event("shutdown")
async def shutdown():
    client.close()


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
