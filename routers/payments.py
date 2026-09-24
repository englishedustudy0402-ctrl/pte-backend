from fastapi import APIRouter, Depends, HTTPException, Request
from middleware.security import get_current_user, get_profile, get_supabase, log_audit, get_client_ip, EXAMS
from pydantic import BaseModel
import razorpay, os, hmac, hashlib
from datetime import datetime, timedelta, timezone

router = APIRouter()

# Three Pro plans: 1 week, 2 weeks, 1 month (price in paise, duration in days)
# One payment unlocks exactly ONE exam (product = per-exam access).
PLANS = {
    "1w":  {"price_paise": 149900, "days": 7,   "label": "1 Week"},
    "2w":  {"price_paise": 249900, "days": 14,  "label": "2 Weeks"},
    "1m":  {"price_paise": 799900, "days": 30,  "label": "1 Month"},
}
DEFAULT_PLAN = "1m"
DEFAULT_EXAM = "pte"
EXAM_LABELS = {
    "pte": "PTE Academic",
    "ielts": "IELTS",
    "det": "Duolingo English Test",
}

def get_rzp():
    return razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))

class CreateOrderRequest(BaseModel):
    plan: str = DEFAULT_PLAN
    exam: str = DEFAULT_EXAM

@router.post("/create-order")
async def create_order(body: CreateOrderRequest, request: Request, profile=Depends(get_profile)):
    rzp = get_rzp()
    supabase = get_supabase()
    ip = get_client_ip(request)

    plan_key = body.plan if body.plan in PLANS else DEFAULT_PLAN
    exam = str(body.exam).lower() if str(body.exam).lower() in EXAMS else DEFAULT_EXAM
    price = PLANS[plan_key]["price_paise"]
    label = PLANS[plan_key]["label"]
    exam_label = EXAM_LABELS.get(exam, exam.upper())

    # Cancel any leftover pending orders: a closed/aborted checkout leaves a
    # pending order behind, and blocking retries for 10 minutes makes the app
    # look broken. A fresh order is always created instead, and the frontend
    # disables its buttons while checkout is actually open.
    supabase.table("subscriptions")\
        .update({"status": "failed"})\
        .eq("user_id", profile["id"])\
        .eq("status", "pending")\
        .execute()

    order = rzp.order.create({
        "amount": price,
        "currency": "INR",
        "receipt": f"{exam}_{profile['id'][:8]}_{int(datetime.now().timestamp())}",
        "notes": {"user_id": profile["id"], "plan": "pro", "period": plan_key, "exam": exam}
    })

    supabase.table("subscriptions").insert({
        "user_id": profile["id"],
        "razorpay_order_id": order["id"],
        "amount_paise": price,
        "status": "pending",
        "plan": "pro",
        "exam": exam,
    }).execute()

    log_audit(profile["id"], "payment_order", {"order_id": order["id"], "plan": plan_key, "exam": exam}, ip)

    return {
        "order_id": order["id"],
        "amount": price,
        "currency": "INR",
        "key_id": os.getenv("RAZORPAY_KEY_ID"),
        "name": "English Edu Study",
        "description": f"{exam_label} Pro Plan — {label} (₹{price // 100})",
        "exam": exam,
        "prefill_email": profile["email"],
        "prefill_name": profile["full_name"],
    }

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@router.post("/verify")
async def verify_payment(body: VerifyPaymentRequest, request: Request, user=Depends(get_current_user)):
    supabase = get_supabase()
    ip = get_client_ip(request)

    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    message = f"{body.razorpay_order_id}|{body.razorpay_payment_id}"
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, body.razorpay_signature):
        log_audit(user.id, "payment_failed", {}, ip)
        raise HTTPException(status_code=400, detail="Invalid signature")

    order_rec = supabase.table("subscriptions")\
        .select("*")\
        .eq("razorpay_order_id", body.razorpay_order_id)\
        .eq("user_id", user.id)\
        .single()\
        .execute()

    if not order_rec.data:
        raise HTTPException(status_code=404, detail="Order not found")

    if order_rec.data["status"] == "paid":
        exam = order_rec.data.get("exam") or DEFAULT_EXAM
        return {"message": "Already activated", "plan": "pro", "exam": exam}

    amount = order_rec.data.get("amount_paise") or 799900
    days = next((p["days"] for p in PLANS.values() if p["price_paise"] == amount), 30)
    exam = str(order_rec.data.get("exam") or DEFAULT_EXAM).lower()
    if exam not in EXAMS:
        exam = DEFAULT_EXAM
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    supabase.table("subscriptions").update({
        "razorpay_payment_id": body.razorpay_payment_id,
        "razorpay_signature": body.razorpay_signature,
        "status": "paid",
        "expires_at": expires_at
    }).eq("razorpay_order_id", body.razorpay_order_id).execute()

    # Per-exam entitlement: one payment = one exam. Legacy 'pro' plan value is
    # kept on the profile so old UI labels still work, but access is granted
    # ONLY via {exam}_expires_at.
    supabase.table("profiles").update({
        "plan": "pro",
        "trial_ends_at": None,
        EXAMS[exam]: expires_at,
    }).eq("id", user.id).execute()

    log_audit(user.id, "payment_success", {"order_id": body.razorpay_order_id, "exam": exam}, ip)

    return {"message": "Payment verified. Pro activated!", "plan": "pro", "exam": exam, "expires_at": expires_at}

@router.get("/status")
async def payment_status(profile=Depends(get_profile)):
    now = datetime.now(timezone.utc)
    access = {}
    for exam, col in EXAMS.items():
        exp = profile.get(col)
        access[exam] = {
            "active": bool(exp) and _parse_expiry(exp) > now,
            "expires_at": exp,
        } if exp else {"active": False, "expires_at": None}
    return {
        "plan": profile["plan"],
        "trial_ends_at": profile.get("trial_ends_at"),
        "exam_access": access,
    }

def _parse_expiry(exp):
    from dateutil import parser
    if isinstance(exp, str):
        exp = parser.parse(exp)
    if exp.tzinfo is None:
        from datetime import timezone as _tz
        exp = exp.replace(tzinfo=_tz.utc)
    return exp