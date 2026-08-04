"""Standalone HTTP flow used by the direct-bind service.

No imports from the parent project, no project database, and no calls to port 5550.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - optional dependency fallback
    curl_requests = None

try:
    import requests as plain_requests
except Exception:  # pragma: no cover - optional dependency fallback
    plain_requests = None

from standalone_core.card_payment import (
    CardPaymentConfig,
    fetch_stripe_publishable_key,
    run_card_payment,
)
from standalone_core.fingerprint_store import (
    extractor_profile,
    get_account_fingerprint,
    get_account_fingerprints,
)
from standalone_core.ph_shortlink_extractor import (
    Mode8Config,
    Mode8Extractor,
    normalize_proxy,
)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "standalone_config.json"
_CONFIG_LOCK = threading.RLock()
_CONFIG_CACHE: dict[str, Any] = {}
_CONFIG_CACHE_MTIME_NS = -1
_PREFLIGHT_LOCK = threading.RLock()
_PREFLIGHT_CACHE: dict[str, tuple[float, str, dict[str, Any]]] = {}
_ACCOUNT_CONTEXT_LOCK = threading.RLock()
_ACCOUNT_CONTEXT_CACHE: dict[str, tuple[float, str, str]] = {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _load_config() -> dict[str, Any]:
    global _CONFIG_CACHE, _CONFIG_CACHE_MTIME_NS
    if not CONFIG_PATH.exists():
        return {}
    mtime_ns = CONFIG_PATH.stat().st_mtime_ns
    with _CONFIG_LOCK:
        if _CONFIG_CACHE and mtime_ns == _CONFIG_CACHE_MTIME_NS:
            return dict(_CONFIG_CACHE)
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig") or "{}")
        if not isinstance(data, dict):
            raise ValueError("standalone_config.json must contain an object")
        _CONFIG_CACHE = data
        _CONFIG_CACHE_MTIME_NS = mtime_ns
        return dict(data)


def _preflight_key(ctx: dict[str, Any]) -> str:
    return f"{ctx['account_id']}\n{ctx['promo_pool'][0]}"


def _remember_preflight(
    ctx: dict[str, Any], result: dict[str, Any], config: dict[str, Any]
) -> None:
    ttl = max(30, min(900, int(config.get("preflight_cache_ttl") or 600)))
    key = _preflight_key(ctx)
    now = time.monotonic()
    with _PREFLIGHT_LOCK:
        _PREFLIGHT_CACHE[key] = (now + ttl, ctx["access_token"], dict(result))
        expired = [item for item, value in _PREFLIGHT_CACHE.items() if value[0] <= now]
        for item in expired:
            _PREFLIGHT_CACHE.pop(item, None)


def _take_preflight(ctx: dict[str, Any]) -> dict[str, Any] | None:
    key = _preflight_key(ctx)
    with _PREFLIGHT_LOCK:
        cached = _PREFLIGHT_CACHE.pop(key, None)
    if not cached or cached[0] <= time.monotonic() or cached[1] != ctx["access_token"]:
        return None
    return dict(cached[2])


def _decode_claims(token: str) -> dict[str, Any]:
    parts = _text(token).split(".")
    if len(parts) < 2:
        raise ValueError("AT format is invalid")
    raw = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except Exception as exc:
        raise ValueError("AT payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("AT payload is invalid")
    return payload


def _account_context(token: str) -> tuple[str, str]:
    claims = _decode_claims(token)
    auth = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    account_id = ""
    if isinstance(auth, dict):
        account_id = _text(auth.get("chatgpt_account_id") or auth.get("account_id"))
    account_id = account_id or _text(claims.get("chatgpt_account_id") or claims.get("account_id"))
    email = _text(profile.get("email") if isinstance(profile, dict) else "") or _text(claims.get("email"))
    cache_key = hashlib.sha256(_text(token).encode("utf-8")).hexdigest()
    if not account_id:
        with _ACCOUNT_CONTEXT_LOCK:
            cached = _ACCOUNT_CONTEXT_CACHE.get(cache_key)
            if cached and cached[0] > time.monotonic():
                account_id = cached[1]
                email = email or cached[2]
    if not account_id:
        raise ValueError("AT does not contain an account id")
    return account_id, email


def _pool(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[\r\n,;]+", value)
    elif isinstance(value, list):
        values = value
    else:
        values = []
    result: list[str] = []
    for item in values:
        raw = _text(item)
        if not raw:
            continue
        proxy = normalize_proxy(raw)
        if not proxy:
            continue
        scheme, endpoint = proxy.split("://", 1)
        fallback_schemes = {
            "http": ("http", "https"),
            "https": ("https", "http"),
            "socks4": ("socks4", "socks4a"),
            "socks4a": ("socks4a", "socks4"),
            "socks5": ("socks5", "socks5h"),
            "socks5h": ("socks5h", "socks5"),
        }.get(scheme.lower(), (scheme.lower(),))
        for candidate_scheme in fallback_schemes:
            candidate = f"{candidate_scheme}://{endpoint}"
            if candidate not in result:
                result.append(candidate)
    return result[:500]


def _is_proxy_transport_error(value: Any) -> bool:
    text = _text(value).lower()
    if any(marker in text for marker in ("card_declined", "incorrect_cvc", "expired_card")):
        return False
    return any(
        marker in text
        for marker in (
            "proxy", "curl: (5)", "curl: (6)", "curl: (7)", "curl: (28)",
            "curl: (35)", "curl: (56)", "connect", "connection", "timed out",
            "timeout", "tls", "ssl", "reset", "remote end closed", "failed to perform",
        )
    )


def _select_account_id(payload: Any) -> str:
    """Select the active account id from the account-check response."""
    if not isinstance(payload, dict):
        return ""
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        return ""
    ordering = payload.get("account_ordering")
    ordered_keys = [str(item) for item in ordering] if isinstance(ordering, list) else []
    ordered_keys.extend(str(key) for key in accounts if str(key) not in ordered_keys)
    fallback = ""
    for key in ordered_keys:
        item = accounts.get(key)
        if not isinstance(item, dict):
            continue
        account = item.get("account")
        nested_id = _text(account.get("account_id")) if isinstance(account, dict) else ""
        candidate = nested_id or ("" if key == "default" else key)
        if not candidate:
            continue
        if item.get("can_access_with_session") is not False:
            return candidate
        fallback = fallback or candidate
    return fallback


def resolve_account(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve a ChatGPT account id from an AT via the account-check endpoint."""
    token = _text(payload.get("access_token"))
    if not token:
        raise ValueError("missing AT")
    claims = _decode_claims(token)
    auth = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    email = _text(profile.get("email") if isinstance(profile, dict) else "") or _text(claims.get("email"))
    user_id = _text(auth.get("user_id") if isinstance(auth, dict) else "") or _text(claims.get("user_id") or claims.get("sub"))
    try:
        account_id, known_email = _account_context(token)
        return {
            "ok": True,
            "account_id": account_id,
            "email": known_email or email,
            "user_id": user_id,
            "source": "jwt_or_cache",
        }
    except ValueError as exc:
        if "account id" not in str(exc).lower():
            raise

    if curl_requests is None and plain_requests is None:
        raise RuntimeError("missing HTTP dependency: install curl_cffi")
    proxy_pool = _pool(payload.get("bind_proxy_pool") or payload.get("proxy_pool"))
    candidates = proxy_pool or [""]
    endpoint = (
        "https://chatgpt.com/backend-api/accounts/check/"
        "v4-2023-04-27?timezone_offset_min=-480"
    )
    device_seed = user_id or email or hashlib.sha256(token.encode("utf-8")).hexdigest()
    device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"direct-bind:{device_seed}"))
    last_error: Exception | None = None
    for proxy in candidates:
        session = (
            curl_requests.Session(impersonate="chrome142")
            if curl_requests is not None
            else plain_requests.Session()
        )
        try:
            if hasattr(session, "trust_env"):
                session.trust_env = False
            session.headers.update({
                "Authorization": f"Bearer {token}",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/142.0.0.0 Safari/537.36"
                ),
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "oai-device-id": device_id,
                "oai-language": "zh-CN",
                "sec-ch-ua": (
                    '"Not_A Brand";v="99", "Chromium";v="142", '
                    '"Google Chrome";v="142"'
                ),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            })
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            response = session.get(endpoint, timeout=30)
            if response.status_code == 401:
                raise ValueError("AT invalidated (HTTP 401)")
            if response.status_code != 200:
                raise RuntimeError(f"account lookup: HTTP {response.status_code}")
            account_id = _select_account_id(response.json())
            if not account_id:
                raise RuntimeError("account lookup returned no account id")
            cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
            with _ACCOUNT_CONTEXT_LOCK:
                _ACCOUNT_CONTEXT_CACHE[cache_key] = (
                    time.monotonic() + 3600,
                    account_id,
                    email,
                )
            return {
                "ok": True,
                "account_id": account_id,
                "email": email,
                "user_id": user_id,
                "source": "account_check",
            }
        except ValueError:
            raise
        except Exception as exc:  # try the next supplied proxy candidate
            last_error = exc
            if not proxy or not _is_proxy_transport_error(exc):
                raise
        finally:
            try:
                session.close()
            except Exception:
                pass
    raise RuntimeError(f"account lookup failed: {last_error or 'unknown error'}")


def _billing_payload(value: Any, *, required: bool) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    fields = {
        key: _text(source.get(key))[:200]
        for key in (
            "name", "email", "phone", "line1", "line2", "city",
            "state", "postal_code", "country",
        )
    }
    fields["country"] = fields["country"].upper()
    fields["state"] = fields["state"].upper()
    fields["postal_code"] = fields["postal_code"].upper()
    if not required:
        return {key: item for key, item in fields.items() if item}
    missing = [
        label
        for key, label in (
            ("name", "持卡人姓名"),
            ("line1", "账单地址"),
            ("city", "城市"),
            ("postal_code", "邮编"),
            ("country", "国家代码"),
        )
        if not fields[key]
    ]
    if missing:
        raise ValueError("账单地址缺少：" + "、".join(missing))
    if not re.fullmatch(r"[A-Z]{2}", fields["country"]):
        raise ValueError("账单国家必须填写两位国家代码")
    if fields["country"] == "US":
        if not fields["state"]:
            raise ValueError("美国账单地址缺少州代码")
        if not re.fullmatch(r"\d{5}(?:-\d{4})?", fields["postal_code"]):
            raise ValueError("美国账单地址 ZIP 格式错误")
    return {key: item for key, item in fields.items() if item}


def validate_payload(payload: dict[str, Any], *, require_payment_method: bool = True) -> dict[str, Any]:
    token = _text(payload.get("access_token"))
    if not token:
        raise ValueError("missing AT")
    identity = resolve_account(payload)
    account_id = _text(identity.get("account_id"))
    email = _text(identity.get("email"))
    bind_pool = _pool(payload.get("bind_proxy_pool"))
    promo_pool = _pool(payload.get("promo_proxy_pool"))
    mode = _text(payload.get("flow_mode") or "full").lower()
    if mode not in {"full", "bind_only", "link_pay", "link_only"}:
        raise ValueError("unsupported flow mode")
    if mode == "link_only":
        if not promo_pool:
            raise ValueError("promo proxy pool is required")
    elif not bind_pool or not promo_pool:
        raise ValueError("both proxy pools are required")
    needs_payment_method = require_payment_method and mode != "link_only"
    payment_method_id = _text(payload.get("payment_method_id"))
    if needs_payment_method and not re.fullmatch(r"pm_[A-Za-z0-9_-]+", payment_method_id):
        raise ValueError("missing or invalid PaymentMethod")
    payment_method_publishable_key = _text(
        payload.get("payment_method_publishable_key")
    )
    if payment_method_publishable_key and not payment_method_publishable_key.startswith(
        ("pk_live_", "pk_test_")
    ):
        raise ValueError("invalid PaymentMethod publishable key")
    return {
        "access_token": token,
        "account_id": account_id,
        "email": email,
        "bind_pool": bind_pool,
        "promo_pool": promo_pool,
        "flow_mode": mode,
        "payment_method_id": payment_method_id,
        "payment_method_publishable_key": payment_method_publishable_key,
        "card_last4": re.sub(r"\D", "", _text(payload.get("card_last4")))[-4:],
        "billing": _billing_payload(
            payload.get("billing"), required=needs_payment_method
        ),
        # The current checkout bundle obtains these values from SentinelSDK
        # immediately before /payments/checkout/confirm.  Keep them attached to
        # the account task so an upstream page/session collector can provide
        # the matching flow-bound pair without putting it in global config.
        "sentinel_token": _text(payload.get("sentinel_token")),
        "telemetry": _text(payload.get("telemetry")),
        # Checkout creation uses `chatgpt_checkout`; payment confirmation uses
        # `checkout_session_approval`.  These tokens are flow-bound.
        "checkout_sentinel_token": _text(payload.get("checkout_sentinel_token")),
        "checkout_telemetry": _text(payload.get("checkout_telemetry")),
    }


def _attach_fingerprint(
    ctx: dict[str, Any],
    config: dict[str, Any],
    logger: Callable[[str], None],
) -> None:
    if ctx.get("fingerprint_profile") and ctx.get("device_id"):
        return
    logger("FLOW_STEP:fingerprint:start:正在加载账号固定指纹")
    region = _text(config.get("fingerprint_region") or "US").upper()
    profile, device_id = get_account_fingerprint(
        ctx["account_id"],
        region=region,
        proxy=ctx["bind_pool"][0] if ctx["bind_pool"] else "",
    )
    adapted = extractor_profile(profile, device_id)
    ctx["fingerprint_profile"] = adapted
    ctx["device_id"] = device_id
    ctx["fingerprint_summary"] = (
        f"{adapted.get('summary', '')} · id={adapted.get('fingerprint_id', '')}"
    ).strip(" ·")
    logger(f"账号指纹已加载：{ctx['fingerprint_summary']}")
    logger(
        f"FLOW_STEP:fingerprint:done:指纹 {adapted.get('fingerprint_id', '')} / TLS {adapted.get('tls_impersonate', '')}"
    )


def _fingerprint_result(
    account_id: str, email: str, profile: Any, device_id: str
) -> dict[str, Any]:
    adapted = extractor_profile(profile, device_id)
    summary = (
        f"{adapted.get('summary', '')} · id={adapted.get('fingerprint_id', '')}"
    ).strip(" ·")
    return {
        "ok": True,
        "email": email,
        "account_id": account_id,
        "fingerprint": summary,
        "fingerprint_id": adapted.get("fingerprint_id", ""),
        "browser": f"{profile.browser}/{profile.browser_full_version}",
        "platform": profile.platform_os,
        "tls": profile.tls_impersonate,
        "language": profile.language,
        "region": profile.region,
        "timezone": profile.timezone_id,
    }


def allocate_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    """Allocate the account's sticky profile without making an upstream call."""
    token = _text(payload.get("access_token"))
    if not token:
        raise ValueError("missing AT")
    identity = resolve_account(payload)
    account_id = _text(identity.get("account_id"))
    email = _text(identity.get("email"))
    config = _load_config()
    region = _text(config.get("fingerprint_region") or "US").upper()
    profile, device_id = get_account_fingerprint(account_id, region=region)
    return _fingerprint_result(account_id, email, profile, device_id)


def allocate_fingerprints(payload: dict[str, Any]) -> dict[str, Any]:
    """Allocate many account profiles with one JSON-store transaction."""
    source = payload.get("accounts")
    if not isinstance(source, list) or not source:
        raise ValueError("accounts must be a non-empty list")
    if len(source) > 500:
        raise ValueError("fingerprint batch cannot exceed 500 accounts")
    config = _load_config()
    region = _text(config.get("fingerprint_region") or "US").upper()
    prepared: list[dict[str, str]] = []
    metadata: list[tuple[str, str, str]] = []
    output: list[dict[str, Any] | None] = [None] * len(source)
    for index, item in enumerate(source):
        client_id = _text(item.get("client_id"))[:100] if isinstance(item, dict) else ""
        try:
            if not isinstance(item, dict):
                raise ValueError("account item must be an object")
            token = _text(item.get("access_token"))
            if not token:
                raise ValueError("missing AT")
            identity = resolve_account(item)
            account_id = _text(identity.get("account_id"))
            email = _text(identity.get("email"))
            prepared.append({"account_id": account_id, "region": region})
            metadata.append((client_id, account_id, email))
        except Exception as exc:  # keep one malformed row from blocking the batch
            output[index] = {"ok": False, "client_id": client_id, "error": str(exc)}
    profiles = get_account_fingerprints(prepared) if prepared else []
    valid_cursor = 0
    for index, item in enumerate(output):
        if item is not None:
            continue
        client_id, account_id, email = metadata[valid_cursor]
        profile, device_id = profiles[valid_cursor]
        output[index] = {
            **_fingerprint_result(account_id, email, profile, device_id),
            "client_id": client_id,
        }
        valid_cursor += 1
    return {"ok": True, "count": len(output), "items": output}


def fetch_billing_address(payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch and normalize one billing address from the configured API."""
    config = _load_config()
    endpoint = _text(config.get("address_generator") or config.get("test_address_generator")) or (
        "https://addressgen.top/api/v1/address?country_code=us&area_code=DE"
    )
    if not endpoint.startswith("https://addressgen.top/api/v1/address?"):
        raise ValueError("地址接口配置错误")
    request = Request(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": "direct-bind-address/1.0"},
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed allowlisted host
        raw = json.loads(response.read().decode("utf-8"))
    data = raw.get("data") if isinstance(raw, dict) else None
    address = data.get("address") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(address, dict):
        raise RuntimeError("测试地址接口返回字段不完整")
    billing = {
        "name": f"{_text(data.get('firstname'))} {_text(data.get('lastname'))}".strip(),
        "line1": _text(address.get("street")),
        "line2": "",
        "city": _text(address.get("city")),
        "state": _text(address.get("area_code")).upper(),
        "postal_code": _text(address.get("zipcode")),
        "country": _text(address.get("country_code") or "US").upper(),
    }
    validated = _billing_payload(billing, required=True)
    return {"ok": True, "source": "address_api", "billing": validated}


def _extract_checkout(ctx: dict[str, Any], config: dict[str, Any], logger: Callable[[str], None]) -> dict[str, Any]:
    country = _text(config.get("country") or "PH").upper()
    currency = _text(config.get("currency") or "PHP").upper()
    # Checkout-link extraction is isolated to proxy pool 2. Proxy pool 1 is
    # reserved for SetupIntent card binding and payment confirmation.
    candidates = list(ctx["promo_pool"][:8])
    last_error: Exception | None = None
    for index, proxy in enumerate(candidates, start=1):
        logger(f"提链代理尝试 {index}/{len(candidates)}")
        try:
            result = Mode8Extractor(
                Mode8Config(
                    token=ctx["access_token"],
                    country=country,
                    currency=currency,
                    promo_campaign=_text(config.get("promo_campaign") or "plus-1-month-free"),
                    require_oaics=True,
                    reject_nonzero=bool(config.get("reject_nonzero", True)),
                    timeout=max(30, min(1800, int(config.get("timeout") or 900))),
                    diagnose_coupon=bool(config.get("coupon_diagnostic", False)),
                    fingerprint_profile=dict(ctx.get("fingerprint_profile") or {}),
                    device_id=_text(ctx.get("device_id")),
                    checkout_sentinel_token=_text(ctx.get("checkout_sentinel_token")),
                    checkout_telemetry=_text(ctx.get("checkout_telemetry")),
                ),
                logger=logger,
            ).run(proxy, proxy)
            checkout_id = _text(result.get("checkout_id"))
            key = _text(result.get("_publishable_key"))
            if not result.get("ok") or not checkout_id.startswith("oaics_") or not key.startswith("pk_"):
                raise RuntimeError("checkout extraction did not return an oaics id and publishable key")
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if index >= len(candidates) or not _is_proxy_transport_error(exc):
                raise
            logger(f"提链代理连接失败，自动切换下一个候选：{exc}")
    raise RuntimeError(f"提链代理池已耗尽：{last_error}")


def preflight(payload: dict[str, Any], logger: Callable[[str], None] = lambda _message: None) -> dict[str, Any]:
    ctx = validate_payload(payload, require_payment_method=False)
    config = _load_config()
    _attach_fingerprint(ctx, config, logger)
    result = _extract_checkout(ctx, config, logger)
    payment_config = CardPaymentConfig(
        token=ctx["access_token"],
        account_id=ctx["account_id"],
        device_id=_text(ctx.get("device_id")),
        fingerprint_profile=dict(ctx.get("fingerprint_profile") or {}),
        timeout=max(30, min(1800, int(config.get("timeout") or 900))),
    )
    bind_candidates = list(ctx["bind_pool"][:8])
    stripe_publishable_key = ""
    last_bind_error: Exception | None = None
    for index, bind_proxy in enumerate(bind_candidates, start=1):
        try:
            stripe_publishable_key = fetch_stripe_publishable_key(
                payment_config,
                proxy=bind_proxy,
                logger=logger,
            )
            break
        except Exception as exc:  # read-only preflight can rotate candidates
            last_bind_error = exc
            if index >= len(bind_candidates) or not _is_proxy_transport_error(exc):
                raise
            logger(
                f"预检绑卡代理 {index}/{len(bind_candidates)} 连接失败，"
                "正在切换下一个候选"
            )
    if not stripe_publishable_key:
        raise RuntimeError(
            "preflight bind proxy pool exhausted "
            f"({len(bind_candidates)} candidates): {last_bind_error or 'unknown error'}"
        )
    _remember_preflight(ctx, result, config)
    fingerprint = dict(ctx.get("fingerprint_profile") or {})
    return {
        "ok": True,
        "publishable_key": stripe_publishable_key,
        "checkout_publishable_key": _text(result.get("_publishable_key")),
        "checkout_id": _text(result.get("checkout_id")),
        "processor_entity": _text(result.get("processor_entity")),
        "country": _text(result.get("country") or config.get("country") or "PH"),
        "currency": _text(result.get("currency") or config.get("currency") or "PHP"),
        "amount": _text(result.get("amount") or "unknown"),
        "link": _text(result.get("url") or result.get("link")),
        "fingerprint": _text(ctx.get("fingerprint_summary")),
        "fingerprint_id": _text(fingerprint.get("fingerprint_id")),
        "fingerprint_tls": _text(fingerprint.get("tls_impersonate")),
        "fingerprint_region": _text(fingerprint.get("region")),
        "fingerprint_timezone": _text(fingerprint.get("timezone")),
    }


def run_flow(payload: dict[str, Any], logger: Callable[[str], None] = lambda _message: None) -> dict[str, Any]:
    ctx = validate_payload(payload, require_payment_method=True)
    config = _load_config()
    _attach_fingerprint(ctx, config, logger)
    first = _take_preflight(ctx)
    if first is None:
        logger("FLOW_STEP:first_link:start:正在执行首次提链")
        first = _extract_checkout(ctx, config, logger)
    else:
        logger("FLOW_STEP:first_link:start:正在复用卡片预检已完成的首次提链")
    logger(
        f"FLOW_STEP:first_link:done:首次提链完成 {_text(first.get('checkout_id'))[:18]}"
    )
    if ctx["flow_mode"] == "link_only":
        return {
            "ok": True,
            "email": ctx["email"],
            "payment_flow": "standalone-http-link-only",
            "flow_complete": True,
            "flow_steps": ["checkout extraction"],
            "checkout_id": _text(first.get("checkout_id")),
            "processor_entity": _text(first.get("processor_entity")),
            "checkout_link": _text(first.get("url") or first.get("link")),
            "fingerprint": _text(ctx.get("fingerprint_summary")),
        }
    timeout = max(30, min(1800, int(config.get("timeout") or 900)))
    country = _text(config.get("country") or "PH").upper()
    currency = _text(config.get("currency") or "PHP").upper()
    billing = dict(config.get("billing") or {})
    billing.update(ctx.get("billing") or {})
    if ctx["email"] and not _text(billing.get("email")):
        billing["email"] = ctx["email"]

    def refresh_checkout() -> dict[str, Any]:
        logger("FLOW_STEP:second_link:start:绑卡后正在重新提链")
        refreshed = _extract_checkout(ctx, config, logger)
        logger(
            f"FLOW_STEP:second_link:done:二次提链完成 {_text(refreshed.get('checkout_id'))[:18]}"
        )
        return refreshed

    bind_only = ctx["flow_mode"] == "bind_only"
    bind_candidates = list(ctx["bind_pool"][:8])
    result: dict[str, Any] | None = None
    last_bind_error: Exception | None = None
    for index, bind_proxy in enumerate(bind_candidates, start=1):
        logger(f"绑卡/支付代理尝试 {index}/{len(bind_candidates)}")
        try:
            result = run_card_payment(
                CardPaymentConfig(
                    token=ctx["access_token"],
                    account_id=ctx["account_id"],
                    country=country,
                    currency=currency,
                    promo_campaign="",
                    billing=billing,
                    payment_method_id=ctx["payment_method_id"],
                    payment_method_publishable_key=ctx[
                        "payment_method_publishable_key"
                    ],
                    card_last4=ctx["card_last4"],
                    flow_mode=ctx["flow_mode"],
                    device_id=_text(ctx.get("device_id")),
                    fingerprint_profile=dict(ctx.get("fingerprint_profile") or {}),
                    locale=_text((ctx.get("fingerprint_profile") or {}).get("oai_language")) or "en-US",
                    timezone=_text((ctx.get("fingerprint_profile") or {}).get("timezone")) or "Asia/Manila",
                    timeout=timeout,
                    checkout_id=_text(first.get("checkout_id")),
                    processor_entity=_text(first.get("processor_entity")),
                    publishable_key=_text(first.get("_publishable_key")),
                    bind_country=_text(config.get("bind_country") or "US").upper(),
                    bind_currency=_text(config.get("bind_currency") or "USD").upper(),
                    strong_bind_direct=True,
                    stop_after_bind=bind_only,
                    # The success loader participates in checkout reconciliation.
                    # Keep the complete callback chain enabled unless a fixture
                    # explicitly opts into the shortened verification path.
                    fast_verify=bool(config.get("fast_verify", False)),
                    sentinel_token=_text(ctx.get("sentinel_token")),
                    telemetry=_text(ctx.get("telemetry")),
                ),
                proxy=bind_proxy,
                logger=logger,
                refresh_checkout=(
                    refresh_checkout if ctx["flow_mode"] == "full" else None
                ),
            )
            break
        except Exception as exc:  # noqa: BLE001
            last_bind_error = exc
            if _text(getattr(exc, "action_required", "")) == "checkout_session_confirmation":
                pending_checkout_id = _text(getattr(exc, "checkout_id", ""))
                pending_processor = _text(getattr(exc, "processor_entity", ""))
                pending_link = _text(getattr(exc, "checkout_link", ""))
                if not pending_link and pending_processor and pending_checkout_id:
                    pending_link = (
                        f"https://chatgpt.com/checkout/{pending_processor}/"
                        f"{pending_checkout_id}"
                    )
                logger("支付确认需要当前 Checkout 会话数据，已保留链接等待继续确认。")
                return {
                    "ok": False,
                    "action_required": "checkout_session_confirmation",
                    "email": ctx["email"],
                    "payment_flow": "standalone-http-awaiting-session-confirmation",
                    "flow_complete": False,
                    "flow_steps": [
                        "checkout extraction",
                        "HTTP card bind / payment-method validation",
                        "checkout session confirmation required",
                    ],
                    "checkout_id": pending_checkout_id,
                    "processor_entity": pending_processor,
                    "checkout_link": pending_link,
                    "card_last4": ctx["card_last4"],
                    "fingerprint": _text(ctx.get("fingerprint_summary")),
                    "detail": str(exc),
                }
            proxy_retry_safe = bool(getattr(exc, "proxy_retry_safe", True))
            if not proxy_retry_safe:
                logger("关键绑卡/支付步骤已提交，为避免重复扣款，不切换代理重跑。")
                raise
            if index >= len(bind_candidates) or not _is_proxy_transport_error(exc):
                raise
            logger(f"绑卡/支付代理连接失败，自动切换下一个候选：{exc}")
    if result is None:
        raise RuntimeError(f"绑卡/支付代理池已耗尽：{last_bind_error}")
    processor = _text(result.get("processor_entity"))
    checkout_id = _text(result.get("checkout_id"))
    link = _text(first.get("url") or first.get("link")) if bind_only else (
        f"https://chatgpt.com/checkout/{processor}/{checkout_id}" if processor and checkout_id else ""
    )
    return {
        "ok": bool(result.get("ok")),
        "email": ctx["email"],
        "payment_flow": "standalone-http-bind" if bind_only else "standalone-http-full",
        "flow_complete": not bind_only,
        "flow_steps": [
            "checkout extraction",
            "HTTP SetupIntent confirmation",
            *( [] if bind_only else ["checkout refresh", "HTTP payment confirmation"] ),
        ],
        "checkout_id": checkout_id,
        "processor_entity": processor,
        "checkout_link": link,
        "card_last4": _text(result.get("card_last4") or ctx["card_last4"]),
        "setup_status": _text(result.get("setup_status")),
        "verify_status": result.get("verify_status", 0),
        "subscription_status": result.get("subscription_status", 0),
        "subscription_plan": _text(result.get("subscription_plan")),
        "fingerprint": _text(ctx.get("fingerprint_summary")),
    }
