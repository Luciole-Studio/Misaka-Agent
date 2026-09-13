"""Pinned Nous entitlement normalization, with no credential-store I/O."""
from __future__ import annotations
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Literal, Optional

NousAccountInfoSource = Literal["jwt", "account_api", "inference_key", "none", "error"]


TOOL_COVERAGE_CATEGORIES = ("firecrawl", "fal", "fal-video", "openai-audio", "browser-use", "modal")


_ACCOUNT_INFO_CACHE_TTL = 60


@dataclass(frozen=True)
class NousPortalSubscriptionInfo:
    plan: Optional[str] = None
    tier: Optional[int] = None
    monthly_charge: Optional[float] = None
    monthly_credits: Optional[float] = None
    current_period_end: Optional[str] = None
    credits_remaining: Optional[float] = None
    rollover_credits: Optional[float] = None


@dataclass(frozen=True)
class NousPaidServiceAccessInfo:
    allowed: Optional[bool] = None
    paid_access: Optional[bool] = None
    reason: Optional[str] = None
    organisation_id: Optional[str] = None
    effective_at_ms: Optional[int] = None
    has_active_subscription: Optional[bool] = None
    active_subscription_is_paid: Optional[bool] = None
    subscription_tier: Optional[int] = None
    subscription_monthly_charge: Optional[float] = None
    subscription_credits_remaining: Optional[float] = None
    purchased_credits_remaining: Optional[float] = None
    total_usable_credits: Optional[float] = None
    member_spend_cap_exceeded: Optional[bool] = None
    member_spend_cap_usd: Optional[float] = None
    member_spend_usd: Optional[float] = None
    member_spend_cap_remaining_usd: Optional[float] = None


@dataclass(frozen=True)
class NousToolAccessInfo:
    """Free tool-pool entitlement (Portal ``tool_access``), decoupled from paid/billing access.

    ``enabled``: a positive pool balance is live and not gated off; ``coverage``: tool category ->
    whether the pool funds it (FAL video is excluded).
    """

    enabled: bool = False
    coverage: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class NousPortalAccountInfo:
    logged_in: bool
    source: NousAccountInfoSource
    fresh: bool
    user_id: Optional[str] = None
    org_id: Optional[str] = None
    org_slug: Optional[str] = None
    org_name: Optional[str] = None
    client_id: Optional[str] = None
    product_id: Optional[str] = None
    nous_client: Optional[str] = None
    portal_base_url: Optional[str] = None
    inference_base_url: Optional[str] = None
    inference_credential_present: bool = False
    credential_source: Optional[str] = None
    expires_at: Optional[datetime] = None
    email: Optional[str] = None
    privy_did: Optional[str] = None
    subscription: Optional[NousPortalSubscriptionInfo] = None
    paid_service_access: Optional[bool] = None
    paid_service_access_info: Optional[NousPaidServiceAccessInfo] = None
    tool_access: Optional[NousToolAccessInfo] = None
    raw_claims: Optional[dict[str, Any]] = None
    raw_account: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    @property
    def is_paid(self) -> bool:
        return self.paid_service_access is True

    @property
    def is_free_tier(self) -> bool:
        return self.paid_service_access is False

    @property
    def tool_gateway_entitled(self) -> bool:
        """Paid access OR a live free tool pool; use ``tool_gateway_entitled_for`` per category."""
        return self.paid_service_access is True or bool(self.tool_access and self.tool_access.enabled)

    def tool_gateway_entitled_for(self, category: str) -> bool:
        """Paid users are entitled everywhere; pool users only where ``coverage[category]`` is true."""
        ta = self.tool_access
        return self.paid_service_access is True or bool(ta and ta.enabled and ta.coverage.get(category) is True)


def _info_from_valid_jwt(
    token: str, state: dict[str, Any], portal_base_url: Optional[str], min_jwt_ttl_seconds: int
) -> Optional[NousPortalAccountInfo]:
    try:
        from .nous_auth import _decode_jwt_claims
    except Exception:
        return None
    claims = _decode_jwt_claims(token)
    if not claims:
        return None
    exp = _coerce_num(claims.get("exp"), float)
    if exp is None or exp <= time.time() + max(0, int(min_jwt_ttl_seconds)):
        return None
    paid_access = _coerce_bool(claims.get("paid_access"))
    access_info = NousPaidServiceAccessInfo(
        allowed=paid_access, paid_access=paid_access, organisation_id=_coerce_str(claims.get("org_id")),
        subscription_tier=_coerce_num(claims.get("subscription_tier"), int),
    )
    return NousPortalAccountInfo(
        logged_in=True, source="jwt", fresh=False,
        user_id=_coerce_str(claims.get("sub")),
        org_id=_coerce_str(claims.get("org_id")),
        client_id=_coerce_str(claims.get("client_id") or state.get("client_id")),
        product_id=_coerce_str(claims.get("product_id")),
        nous_client=_coerce_str(claims.get("nous_client")),
        portal_base_url=portal_base_url,
        inference_base_url=_coerce_str(state.get("inference_base_url")),
        inference_credential_present=True,
        credential_source=_coerce_str(state.get("credential_source")) or "auth_store",
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
        paid_service_access=paid_access, paid_service_access_info=access_info,
        tool_access=_tool_access_from_value(claims.get("tool_access")),
        raw_claims=dict(claims),
    )


def _info_from_account_payload(
    payload: dict[str, Any], *, state: dict[str, Any], portal_base_url: Optional[str]
) -> NousPortalAccountInfo:
    user = _dict_or_empty(payload.get("user"))
    organisation = _dict_or_empty(payload.get("organisation"))
    access = _coerced_dataclass(NousPaidServiceAccessInfo, payload.get("paid_service_access"))
    paid_access = None
    if access is not None:
        paid_access = access.allowed if access.allowed is not None else access.paid_access
    return NousPortalAccountInfo(
        logged_in=True, source="account_api", fresh=True,
        org_id=_coerce_str(organisation.get("id")) or (access.organisation_id if access else None),
        org_slug=_coerce_str(organisation.get("slug")),
        org_name=_coerce_str(organisation.get("name")),
        client_id=_coerce_str(state.get("client_id")),
        portal_base_url=portal_base_url,
        inference_base_url=_coerce_str(state.get("inference_base_url")),
        inference_credential_present=bool(state.get("access_token") or state.get("agent_key")),
        credential_source=_coerce_str(state.get("credential_source")) or "auth_store",
        email=_coerce_str(user.get("email")),
        privy_did=_coerce_str(user.get("privy_did")),
        subscription=_subscription_from_payload(payload.get("subscription")),
        paid_service_access=paid_access, paid_service_access_info=access,
        tool_access=_tool_access_from_value(payload.get("tool_access")),
        raw_account=dict(payload),
    )


def _tool_access_from_value(value: Any) -> Optional[NousToolAccessInfo]:
    """Parse a Portal ``tool_access`` object (JWT claim or account API).

    Fails closed: a non-object yields ``None``; only literal ``true`` counts for ``enabled`` and
    each coverage entry.
    """
    if not isinstance(value, dict):
        return None
    coverage = {k: v is True for k, v in _dict_or_empty(value.get("coverage")).items() if isinstance(k, str)}
    return NousToolAccessInfo(enabled=value.get("enabled") is True, coverage=coverage)


def _coerced_dataclass(cls, value: Any):
    """Build ``cls`` from a payload dict (field names = payload keys), coercing by declared type."""
    return cls(**{f.name: _COERCERS[f.type](value.get(f.name)) for f in fields(cls)}) if isinstance(value, dict) else None


def _subscription_from_payload(value: Any) -> Optional[NousPortalSubscriptionInfo]:
    return _coerced_dataclass(NousPortalSubscriptionInfo, value)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _coerce_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _coerce_num(value: Any, cast):
    """``cast(value)`` or None; bools and None are rejected, not coerced."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


_COERCERS = {
    "Optional[str]": _coerce_str,
    "Optional[bool]": _coerce_bool,
    "Optional[int]": lambda v: _coerce_num(v, int),
    "Optional[float]": lambda v: _coerce_num(v, float),
}
