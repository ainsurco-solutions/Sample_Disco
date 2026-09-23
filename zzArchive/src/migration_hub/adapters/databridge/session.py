from __future__ import annotations

import httpx

from migration_hub.adapters.base import PlatformSession
from migration_hub.adapters.databridge.errors import (
    AuthenticationError,
    DatabridgeError,
    RateLimitedError,
)

def resolve(*, host: str, api_key: str, entitlement: str = "RI-DATAVAULT") -> PlatformSession:
    with httpx.Client(base_url=host, headers={"Authorization": api_key}, timeout=30.0) as client:
        response = client.get(f"/platform/tenantdata/v1/entitlements/{entitlement}/resourcegroups")
        _raise_for_status(response)
        resource_groups = response.json()
        if not resource_groups:
            raise DatabridgeError(f"No resource groups returned for entitlement {entitlement!r}")
        resource_group_id = resource_groups[0]["resourceGroupId"]

    return PlatformSession(resource_group_id=resource_group_id)

def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise AuthenticationError(f"401 from {response.request.url}")
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(
            f"429 from {response.request.url}",
            retry_after=float(retry_after) if retry_after else None,
        )
    if response.status_code >= 400:
        raise DatabridgeError(f"{response.status_code} from {response.request.url}: {response.text}")
