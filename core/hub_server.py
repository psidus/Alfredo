"""
Alfredo Hub API — registry for sharing workflow packages.

Run: python -c "from core.hub_server import start_hub_server; start_hub_server()"
Port: HUB_PORT (default 8010)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from core.hub_db import HubDB
from core.workflow_package import package_checksum, sanitize_package_for_share, validate_package

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Alfredo Workflow Hub",
    description="Local-first registry for sharing Alfredo workflow packages.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_hub() -> HubDB:
    return HubDB()


def _hub_registration_mode() -> str:
    mode = (os.getenv("HUB_REGISTRATION") or "open").strip().lower()
    return mode if mode in ("open", "invite", "closed") else "open"


def _hub_required_org() -> str:
    return (os.getenv("HUB_REQUIRED_ORG") or "").strip().lower()


def _hub_allow_public() -> bool:
    return (os.getenv("HUB_ALLOW_PUBLIC") or "true").strip().lower() in ("1", "true", "yes")


def _validate_registration(req: "RegisterRequest") -> str:
    """Return org_slug to use after policy checks. Raises HTTPException on failure."""
    mode = _hub_registration_mode()
    if mode == "closed":
        raise HTTPException(status_code=403, detail="Hub registration is closed")

    if mode == "invite":
        expected = (os.getenv("HUB_INVITE_TOKEN") or "").strip()
        if not expected:
            raise HTTPException(status_code=503, detail="Hub invite token is not configured")
        provided = (req.invite_token or "").strip()
        if provided != expected:
            raise HTTPException(status_code=403, detail="Invalid invite token")

    required_org = _hub_required_org()
    if required_org:
        return required_org
    return (req.org_slug or "").strip().lower()


def _validate_visibility(visibility: str) -> str:
    visibility = (visibility or "private").lower()
    if visibility not in ("private", "org", "public"):
        raise HTTPException(status_code=400, detail="visibility must be private|org|public")
    if visibility == "public" and not _hub_allow_public():
        raise HTTPException(status_code=403, detail="Public packages are disabled on this hub")
    return visibility


# --- models ---

class RegisterRequest(BaseModel):
    username: str
    display_name: str = ""
    org_slug: str = ""
    invite_token: str = ""


class RegisterResponse(BaseModel):
    username: str
    display_name: str
    org_slug: str
    token: str
    message: str = "Store this token in HUB_TOKEN — it will not be shown again."


class PublishRequest(BaseModel):
    slug: str
    title: str
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    visibility: str = "private"  # private | org | public
    package_version: str = "1.0.0"
    package: Dict[str, Any]
    share_with: List[str] = Field(default_factory=list)


class ShareRequest(BaseModel):
    username: str


class PackageSummary(BaseModel):
    id: int
    slug: str
    title: str
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    visibility: str
    author_username: Optional[str] = None
    package_version: str = "1.0.0"
    downloads: int = 0
    checksum: str = ""


def _auth_user(
    x_hub_username: Optional[str] = Header(None, alias="X-Hub-Username"),
    x_hub_token: Optional[str] = Header(None, alias="X-Hub-Token"),
    hub: HubDB = Depends(get_hub),
) -> Optional[Dict[str, Any]]:
    if not x_hub_username or not x_hub_token:
        return None
    user = hub.authenticate(x_hub_username, x_hub_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid hub credentials")
    return user


def _require_user(user: Optional[Dict[str, Any]] = Depends(_auth_user)) -> Dict[str, Any]:
    if not user:
        raise HTTPException(status_code=401, detail="X-Hub-Username and X-Hub-Token required")
    return user


@app.get("/hub/health")
def health():
    required_org = _hub_required_org()
    return {
        "status": "ok",
        "service": "alfredo-hub",
        "version": "1.0.0",
        "registration_mode": _hub_registration_mode(),
        "required_org": required_org or None,
        "public_packages_allowed": _hub_allow_public(),
    }


@app.post("/hub/register", response_model=RegisterResponse)
def register(req: RegisterRequest, hub: HubDB = Depends(get_hub)):
    org_slug = _validate_registration(req)
    try:
        user, token = hub.register_user(req.username, req.display_name, org_slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RegisterResponse(
        username=user["username"],
        display_name=user.get("display_name") or user["username"],
        org_slug=user.get("org_slug") or "",
        token=token,
    )


@app.post("/hub/token/rotate")
def rotate_token(user: Dict[str, Any] = Depends(_require_user), hub: HubDB = Depends(get_hub)):
    token = hub.rotate_token(user["id"])
    return {"token": token, "message": "Update HUB_TOKEN with this value."}


@app.post("/hub/packages")
def publish(req: PublishRequest, user: Dict[str, Any] = Depends(_require_user), hub: HubDB = Depends(get_hub)):
    visibility = _validate_visibility(req.visibility)
    # Strip any accidental credentials before storing on the hub
    safe_package = sanitize_package_for_share(req.package)
    errors = validate_package(safe_package)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    checksum = safe_package.get("checksum") or package_checksum(safe_package)
    try:
        pkg = hub.publish_package(
            user,
            slug=req.slug.strip().lower(),
            title=req.title,
            description=req.description,
            tags=req.tags,
            visibility=visibility,
            package_json=safe_package,
            checksum=checksum,
            package_version=req.package_version,
            format_version=str(safe_package.get("format_version") or "1.0"),
            share_usernames=req.share_with,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    pkg["author_username"] = user["username"]
    return pkg


@app.get("/hub/packages")
def search(
    q: str = "",
    tag: str = "",
    limit: int = 50,
    user: Optional[Dict[str, Any]] = Depends(_auth_user),
    hub: HubDB = Depends(get_hub),
):
    return hub.search_packages(query=q, tag=tag, user=user, limit=min(limit, 100))


@app.get("/hub/packages/{package_id}")
def get_package(
    package_id: int,
    user: Optional[Dict[str, Any]] = Depends(_auth_user),
    hub: HubDB = Depends(get_hub),
):
    pkg = hub.get_package(package_id, include_body=True)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")
    if not hub.user_can_access(pkg, user):
        raise HTTPException(status_code=403, detail="Access denied")
    hub.increment_downloads(package_id)
    return pkg


@app.post("/hub/packages/{package_id}/share")
def share(
    package_id: int,
    req: ShareRequest,
    user: Dict[str, Any] = Depends(_require_user),
    hub: HubDB = Depends(get_hub),
):
    try:
        hub.share_package(package_id, user["id"], req.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "shared_with": req.username}


@app.delete("/hub/packages/{package_id}")
def delete_package(
    package_id: int,
    user: Dict[str, Any] = Depends(_require_user),
    hub: HubDB = Depends(get_hub),
):
    try:
        hub.delete_package(package_id, user["id"])
    except ValueError as e:
        msg = str(e)
        code = 404 if "not found" in msg.lower() else 403
        raise HTTPException(status_code=code, detail=msg)
    return {"ok": True, "deleted_id": package_id}


def start_hub_server(host: str = "0.0.0.0", port: int = None):
    import uvicorn
    port = port or int(os.getenv("HUB_PORT", "8010"))
    logger.info(f"Starting Alfredo Hub on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_hub_server()
