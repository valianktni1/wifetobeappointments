import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import logging
import smtplib
from datetime import datetime, timezone, timedelta, date, time as dtime
from email.message import EmailMessage
from typing import List, Optional, Annotated, Any

import bcrypt
import jwt
import pyotp
import qrcode
import io
import base64
from bson import ObjectId
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request
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
async def get_settings() -> dict:
    s = await db.settings.find_one({"_id": "global"})
    if not s:
        s = {
            "_id": "global",
            "business_email": "",
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_user": "",
            "smtp_password": "",
            "from_name": "Wife To Be",
            "notify_customer_on_booking": False,
            "notify_shop_on_booking": False,
            "notify_on_confirm": False,
        }
        await db.settings.insert_one(s)
    return s


def send_email(settings: dict, to: str, subject: str, body: str) -> bool:
    if not settings.get("smtp_host") or not settings.get("business_email"):
        logger.info("Email skipped (SMTP not configured): %s -> %s", subject, to)
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{settings.get('from_name', 'Wife To Be')} <{settings['business_email']}>"
        msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP(settings["smtp_host"], int(settings.get("smtp_port", 587)), timeout=15) as server:
            server.starttls()
            if settings.get("smtp_user"):
                server.login(settings["smtp_user"], settings.get("smtp_password", ""))
            server.send_message(msg)
        return True
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return False


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


class DayHours(BaseModel):
    closed: bool = False
    open: str = "10:00"
    close: str = "17:00"


class AvailabilityIn(BaseModel):
    hours: dict  # {"0": {closed, open, close}, ...}
    slot_step: int = 30


class BlockedDateIn(BaseModel):
    date: str  # YYYY-MM-DD
    reason: Optional[str] = ""


class BookingIn(BaseModel):
    shop_id: str
    appointment_type_id: str
    date: str  # YYYY-MM-DD
    start_time: str  # HH:MM
    customer_name: str
    customer_email: EmailStr
    customer_phone: str
    notes: Optional[str] = ""


class BookingUpdateIn(BaseModel):
    status: Optional[str] = None  # confirmed / cancelled / completed / pending
    date: Optional[str] = None
    start_time: Optional[str] = None
    admin_notes: Optional[str] = None


class SettingsIn(BaseModel):
    business_email: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    from_name: Optional[str] = None
    notify_customer_on_booking: Optional[bool] = None
    notify_shop_on_booking: Optional[bool] = None
    notify_on_confirm: Optional[bool] = None


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
    if body.duration not in (30, 60, 90, 120):
        raise HTTPException(status_code=400, detail="Duration must be 30, 60, 90 or 120 minutes")
    doc = body.model_dump()
    doc["shop_id"] = shop_id
    res = await db.appointment_types.insert_one(doc)
    doc["_id"] = res.inserted_id
    return clean(doc)


@api.patch("/appointment-types/{type_id}")
async def update_type(type_id: str, body: AppointmentTypeIn, user: dict = Depends(get_current_user)):
    if body.duration not in (30, 60, 90, 120):
        raise HTTPException(status_code=400, detail="Duration must be 30, 60, 90 or 120 minutes")
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
        return {"shop_id": shop_id, "hours": {}, "slot_step": 30}
    doc.pop("_id", None)
    return doc


@api.put("/shops/{shop_id}/availability")
async def set_availability(shop_id: str, body: AvailabilityIn, user: dict = Depends(get_current_user)):
    await db.availability.update_one(
        {"shop_id": shop_id},
        {"$set": {"shop_id": shop_id, "hours": body.hours, "slot_step": body.slot_step}},
        upsert=True,
    )
    return {"ok": True}


@api.get("/shops/{shop_id}/blocked-dates")
async def get_blocked(shop_id: str):
    docs = await db.blocked_dates.find({"shop_id": shop_id}).sort("date", 1).to_list(365)
    return [clean(d) for d in docs]


@api.post("/shops/{shop_id}/blocked-dates")
async def add_blocked(shop_id: str, body: BlockedDateIn, user: dict = Depends(get_current_user)):
    doc = {"shop_id": shop_id, "date": body.date, "reason": body.reason}
    res = await db.blocked_dates.insert_one(doc)
    doc["_id"] = res.inserted_id
    return clean(doc)


@api.delete("/blocked-dates/{block_id}")
async def del_blocked(block_id: str, user: dict = Depends(get_current_user)):
    await db.blocked_dates.delete_one({"_id": oid(block_id)})
    return {"ok": True}


# ------------------------------------------------------------------ slot computation
def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@api.get("/public/slots")
async def public_slots(shop_id: str, date: str, duration: int):
    if duration not in (30, 60, 90, 120):
        raise HTTPException(status_code=400, detail="Invalid duration")
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")
    if d < datetime.now().date():
        return {"slots": []}
    blocked = await db.blocked_dates.find_one({"shop_id": shop_id, "date": date})
    if blocked:
        return {"slots": [], "reason": "closed"}
    avail = await db.availability.find_one({"shop_id": shop_id})
    if not avail:
        return {"slots": []}
    weekday = str(d.weekday())  # Mon=0
    day = avail.get("hours", {}).get(weekday)
    if not day or day.get("closed"):
        return {"slots": [], "reason": "closed"}
    step = avail.get("slot_step", 30)
    open_m, close_m = _to_min(day["open"]), _to_min(day["close"])
    # existing bookings for this shop/date (not cancelled)
    existing = await db.bookings.find(
        {"shop_id": shop_id, "date": date, "status": {"$ne": "cancelled"}}
    ).to_list(200)
    busy = [(_to_min(b["start_time"]), _to_min(b["start_time"]) + b["duration"]) for b in existing]
    slots = []
    t = open_m
    while t + duration <= close_m:
        s_start, s_end = t, t + duration
        overlap = any(s_start < be and s_end > bs for bs, be in busy)
        if not overlap:
            slots.append(_to_hhmm(t))
        t += step
    return {"slots": slots}


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
    # validate slot still available
    avail = await public_slots(shop_id=body.shop_id, date=body.date, duration=duration)
    if body.start_time not in avail.get("slots", []):
        raise HTTPException(status_code=409, detail="That time slot is no longer available")
    ref = "WTB-" + base64.b32encode(os.urandom(5)).decode().rstrip("=")[:8]
    doc = {
        "reference": ref,
        "shop_id": body.shop_id,
        "shop_name": shop["name"],
        "appointment_type_id": body.appointment_type_id,
        "appointment_type_name": atype["name"],
        "duration": duration,
        "date": body.date,
        "start_time": body.start_time,
        "customer_name": body.customer_name.strip(),
        "customer_email": body.customer_email.lower().strip(),
        "customer_phone": body.customer_phone.strip(),
        "notes": body.notes,
        "admin_notes": "",
        "status": "pending",
        "created_at": now_utc().isoformat(),
    }
    res = await db.bookings.insert_one(doc)
    doc["_id"] = res.inserted_id
    # optional notifications
    settings = await get_settings()
    when = f"{body.date} at {body.start_time}"
    if settings.get("notify_customer_on_booking"):
        send_email(settings, doc["customer_email"], "Your Wife To Be appointment request",
                   f"Dear {doc['customer_name']},\n\nWe have received your request for a {atype['name']} at our {shop['name']} boutique on {when}. "
                   f"Your reference is {ref}. We will confirm your appointment shortly.\n\nWith love,\nWife To Be")
    if settings.get("notify_shop_on_booking") and settings.get("business_email"):
        send_email(settings, settings["business_email"], f"New booking request — {shop['name']}",
                   f"{doc['customer_name']} ({doc['customer_email']}, {doc['customer_phone']}) requested a {atype['name']} on {when}. Ref {ref}.")
    return clean(doc)


@api.get("/public/bookings/{reference}")
async def get_booking_by_ref(reference: str):
    doc = await db.bookings.find_one({"reference": reference})
    if not doc:
        raise HTTPException(status_code=404, detail="Booking not found")
    return clean(doc)


@api.get("/bookings")
async def list_bookings(user: dict = Depends(get_current_user), shop_id: Optional[str] = None,
                        status: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None):
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
    docs = await db.bookings.find(query).sort([("date", 1), ("start_time", 1)]).to_list(1000)
    return [clean(d) for d in docs]


@api.patch("/bookings/{booking_id}")
async def update_booking(booking_id: str, body: BookingUpdateIn, user: dict = Depends(get_current_user)):
    booking = await db.bookings.find_one({"_id": oid(booking_id)})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    update = {}
    if body.status:
        if body.status not in ("pending", "confirmed", "cancelled", "completed"):
            raise HTTPException(status_code=400, detail="Invalid status")
        update["status"] = body.status
    if body.date:
        update["date"] = body.date
    if body.start_time:
        update["start_time"] = body.start_time
    if body.admin_notes is not None:
        update["admin_notes"] = body.admin_notes
    if update:
        await db.bookings.update_one({"_id": booking["_id"]}, {"$set": update})
    doc = await db.bookings.find_one({"_id": booking["_id"]})
    if body.status == "confirmed":
        settings = await get_settings()
        if settings.get("notify_on_confirm"):
            send_email(settings, doc["customer_email"], "Your Wife To Be appointment is confirmed",
                       f"Dear {doc['customer_name']},\n\nYour {doc['appointment_type_name']} at our {doc['shop_name']} boutique on "
                       f"{doc['date']} at {doc['start_time']} is now confirmed. Reference {doc['reference']}.\n\nWe can't wait to see you.\nWife To Be")
    return clean(doc)


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
    return {
        "total": total, "pending": pending, "confirmed": confirmed, "upcoming": upcoming,
        "today": [clean(d) for d in today_list], "per_shop": per_shop,
    }


# ------------------------------------------------------------------ settings
@api.get("/settings")
async def read_settings(user: dict = Depends(require_superadmin)):
    s = await get_settings()
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
    # superadmin
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

    if await db.shops.count_documents({}) == 0:
        warr = {
            "name": "Warrington Boutique", "slug": "warrington", "role_label": "Wedding Dresses",
            "address": "3-5 Fennel Street, Warrington, WA1 2PA", "phone": "01925 570093",
            "email": "thegroupuk@yahoo.com", "order": 0,
            "blurb": "Home to over 200 designer wedding gowns — a private, unhurried styling experience.",
        }
        runc = {
            "name": "Runcorn Boutique", "slug": "runcorn", "role_label": "Suit Hire",
            "address": "136 Greenway Road, Runcorn, WA7 5BS", "phone": "0151 420 0151",
            "email": "thegroupuk@yahoo.com", "order": 1,
            "blurb": "Men's formal wear & suit hire, by appointment only.",
        }
        rw = await db.shops.insert_one(warr)
        rr = await db.shops.insert_one(runc)
        warr_id, runc_id = str(rw.inserted_id), str(rr.inserted_id)
        # availability: Warrington Tue-Fri 11-17, Sat 10-17
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
        await db.availability.insert_one({"shop_id": warr_id, "hours": warr_hours, "slot_step": 30})
        await db.availability.insert_one({"shop_id": runc_id, "hours": runc_hours, "slot_step": 30})
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


@app.on_event("startup")
async def startup():
    await seed()


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
