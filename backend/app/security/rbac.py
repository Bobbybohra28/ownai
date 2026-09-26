"""Role-based access control.

Organization roles grant permissions; every tenant-owned resource is additionally
scoped by ``organization_id`` in the repositories (organization isolation).
"""

from __future__ import annotations

from enum import StrEnum

from app.core.exceptions import PermissionDenied
from app.database.models import OrgRole


class Permission(StrEnum):
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    PROJECT_DELETE = "project:delete"
    CHAT = "chat"
    RUN_EXECUTE = "run:execute"          # sandboxed execution (tests, python)
    APPROVAL_DECIDE = "approval:decide"  # approve destructive actions
    SQL_CONNECT = "sql:connect"
    MEMBERS_MANAGE = "members:manage"
    MODELS_MANAGE = "models:manage"
    AGENTS_MANAGE = "agents:manage"
    EVAL_RUN = "eval:run"
    AUDIT_READ = "audit:read"
    BILLING_MANAGE = "billing:manage"


_VIEWER = {Permission.PROJECT_READ}
_MEMBER = _VIEWER | {
    Permission.PROJECT_WRITE,
    Permission.CHAT,
    Permission.RUN_EXECUTE,
    Permission.APPROVAL_DECIDE,
    Permission.SQL_CONNECT,
}
_ADMIN = _MEMBER | {
    Permission.PROJECT_DELETE,
    Permission.MEMBERS_MANAGE,
    Permission.MODELS_MANAGE,
    Permission.AGENTS_MANAGE,
    Permission.EVAL_RUN,
    Permission.AUDIT_READ,
}
_OWNER = _ADMIN | {Permission.BILLING_MANAGE}

ROLE_PERMISSIONS: dict[OrgRole, frozenset[Permission]] = {
    OrgRole.VIEWER: frozenset(_VIEWER),
    OrgRole.MEMBER: frozenset(_MEMBER),
    OrgRole.ADMIN: frozenset(_ADMIN),
    OrgRole.OWNER: frozenset(_OWNER),
}


def has_permission(role: OrgRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def require_permission(role: OrgRole, permission: Permission) -> None:
    if not has_permission(role, permission):
        raise PermissionDenied(f"Your role ({role.value}) does not allow this action ({permission.value}).")
