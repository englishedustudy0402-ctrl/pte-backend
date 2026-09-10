from fastapi import APIRouter, Depends, HTTPException

from middleware.security import get_supabase, require_role, log_audit

router = APIRouter(tags=["admin"])


def _list(profile, status):
    supabase = get_supabase()
    query = (
        supabase.table("profiles")
        .select("id,email,full_name,phone,plan,approval_status,role,created_at")
        .order("created_at", desc=True)
        .limit(200)
    )
    if status and status != "all":
        query = query.eq("approval_status", status)
    result = query.execute()
    return result.data


@router.get("/admin/profiles")
async def list_profiles(status: str = "pending", admin=Depends(require_role("admin"))):
    """Admin-only listing of accounts, filtered by approval_status."""
    return _list(admin, status)


@router.patch("/admin/profiles/{user_id}/approve")
async def approve_user(user_id: str, admin=Depends(require_role("admin"))):
    result = (
        get_supabase().table("profiles").update({"approval_status": "approved"})
        .eq("id", user_id).execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")
    log_audit(admin["id"], "approve_user", {"user_id": user_id})
    return {"ok": True, "user_id": user_id, "approval_status": "approved"}


@router.patch("/admin/profiles/{user_id}/reject")
async def reject_user(user_id: str, admin=Depends(require_role("admin"))):
    result = (
        get_supabase().table("profiles").update({"approval_status": "rejected"})
        .eq("id", user_id).execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")
    log_audit(admin["id"], "reject_user", {"user_id": user_id})
    return {"ok": True, "user_id": user_id, "approval_status": "rejected"}