"""Small import seam so policy tests do not need the full LiteLLM distribution."""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised inside the pinned gateway image
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
except ImportError:  # unit-test fallback with the same attribute surface

    class LitellmUserRoles:  # type: ignore[no-redef]
        PROXY_ADMIN = "proxy_admin"
        INTERNAL_USER = "internal_user"

    class UserAPIKeyAuth:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any):
            self.__dict__.update(kwargs)


__all__ = ["LitellmUserRoles", "UserAPIKeyAuth"]
