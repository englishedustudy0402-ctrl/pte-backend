from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client
from functools import lru_cache
import os, logging
from datetime import datetime, timezone
from slowapi import Limiter
from slowapi.util import get_remote_address

# 1. Initialize Limiter here so all routers can import it
limiter = Limiter(key_func=get_remote_address)
logger = logging.getLogger(__name__)
security = HTTPBearer()

@lru_cache()
def get_supabase():
    # Note: Ensure .env uses SUPABASE_SERVICE_ROLE_KEY for admin tasks like auditing
    return create_client(
        os.getenv("SUPABASE_URL"), 
        os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    )

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    supabase = get_supabase()
    try:
        user_response = supabase.auth.get_user(credentials.credentials)
        if not user_response or not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_response.user
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("token verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Authentication required")

async def get_profile(user=Depends(get_current_user)):
    supabase = get_supabase()
    result = supabase.table("profiles").select("*").eq("id", user.id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return result.data


def _is_trial_active(profile) -> bool:
    """Trial is active while trial_ends_at is in the future."""
    trial_end = profile.get("trial_ends_at")
    if not trial_end:
        return False
    from dateutil import parser
    if isinstance(trial_end, str):
        trial_end = parser.parse(trial_end)
    if trial_end.tzinfo is None:
        trial_end = trial_end.replace(tzinfo=timezone.utc)
    return trial_end > datetime.now(timezone.utc)


async def require_authenticated(profile=Depends(get_profile)):
    """Any signed-in, non-banned, admin-approved user. Accounts that are still
    pending (or were rejected) are blocked from ALL practice-facing endpoints
    until an admin approves them."""
    if profile.get("is_banned"):
        raise HTTPException(status_code=403, detail="Account suspended")
    status = profile.get("approval_status")
    if status is not None and status != "approved":
        raise HTTPException(status_code=403, detail="Account pending admin approval")
    return profile


async def optional_profile(request: Request) -> dict | None:
    """Like get_profile but requires no token: returns the profile when a valid
    Bearer token is present, otherwise None. Used to let scoring work for
    guests while still attaching the owner when a session exists."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    try:
        supabase = get_supabase()
        user = supabase.auth.get_user(token)
        if not user or not user.user:
            return None
        result = supabase.table("profiles").select("*").eq("id", user.user.id).single().execute()
        if result.data and not result.data.get("is_banned"):
            return result.data
    except Exception as e:
        logger.warning("optional auth failed: %s", e)
    return None


# Supported exams. Each maps to a per-exam expiry column on profiles, so a
# single payment unlocks exactly ONE exam (the product is "1 payment = 1 exam").
EXAMS = {
    "pte": "pte_expires_at",
    "ielts": "ielts_expires_at",
    "det": "det_expires_at",
}

def _parse_dt(value):
    from dateutil import parser
    if isinstance(value, str):
        value = parser.parse(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def has_exam_access(profile, exam: str) -> bool:
    """Whether the profile currently holds live access to a specific exam.

    Admin always passes (owns the platform). Paid access is per-exam via
    {exam}_expires_at on profiles. No free trial bypass: every non-admin
    account must purchase an exam to unlock its premium bank (condensed free
    questions stay behind require_exam_access on the routers' free path).
    """
    if (profile.get("email") or "").strip().lower() == ADMIN_EMAIL.strip().lower():
        return True
    if profile.get("is_banned"):
        return False
    col = EXAMS.get(str(exam).lower())
    if not col:
        return False
    exp = profile.get(col)
    if not exp:
        return False
    try:
        return _parse_dt(exp) > datetime.now(timezone.utc)
    except Exception:
        return False


def require_exam_access(exam: str):
    """Dependency factory: enforce live access to ONE exam (e.g. 'pte')."""
    async def _check(profile=Depends(require_authenticated)):
        if not has_exam_access(profile, exam):
            col = EXAMS.get(str(exam).lower())
            hint = f"{exam.upper()} subscription required or expired"
            if col and profile.get(col):
                # Persist the downgrade so the client can't replay past expiry.
                supabase = get_supabase()
                supabase.table("profiles").update({"plan": "free"}).eq("id", profile["id"]).execute()
            raise HTTPException(status_code=402, detail=hint)
        return profile
    return _check


async def require_active_plan(profile=Depends(require_authenticated)):
    """Legacy all-exams gate: any paid exam entitlement. Admin always passes;
    trial no longer counts (must purchase). Kept for endpoints until they are
    exam-scoped."""
    for exam in EXAMS:
        if has_exam_access(profile, exam):
            return profile
    if profile.get("plan") == "pro":
        return profile
    raise HTTPException(status_code=402, detail="Subscription required")


ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "englishedustudy0402@gmail.com")

def require_role(*roles: str):
    """Dependency factory: enforce application roles (e.g. 'admin'). Admin
    access is additionally locked to ADMIN_EMAIL so only that one account can
    manage approvals, regardless of the role flag on other profiles."""
    async def _check(profile=Depends(require_authenticated)):
        role = profile.get("role") or "member"
        if role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        if "admin" in roles:
            email = (profile.get("email") or "").strip().lower()
            if email != ADMIN_EMAIL.strip().lower():
                raise HTTPException(status_code=403, detail="Insufficient permissions")
        return profile
    return _check

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def log_audit(user_id: str, action: str, metadata: dict = None, ip: str = None):
    try:
        supabase = get_supabase()
        supabase.table("audit_log").insert({
            "user_id": user_id,
            "action": action,
            "metadata": metadata or {},
            "ip_address": ip
        }).execute()
    except Exception as e:
        logger.error(f"Audit log failed: {e}")