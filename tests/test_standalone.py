from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from server import (
    TASKS,
    Handler,
    _fail_running_step,
    _initial_steps,
    _checkout_link,
    _run_task,
    _safe_error,
    _set_task,
    _update_step,
)
from standalone_flow import (
    _extract_checkout,
    _select_account_id,
    allocate_fingerprint,
    allocate_fingerprints,
    fetch_billing_address,
    preflight,
    run_flow,
    validate_payload,
)
from standalone_core import card_payment
from standalone_core.fingerprint_store import (
    audit_fingerprint,
    extractor_profile,
    get_account_fingerprint,
    optimize_fingerprint_store,
)
from standalone_core.register_profile import choose_tls_impersonate, profile_to_json
from standalone_core.ph_shortlink_extractor import build_session, normalize_proxy

ROOT = Path(__file__).resolve().parents[1]


def fixture_token() -> str:
    payload = {
        "https://api.openai.com/auth": {"chatgpt_account_id": "account_fixture"},
        "https://api.openai.com/profile": {"email": "fixture@example.test"},
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{raw}.signature"


def fixture_billing() -> dict[str, str]:
    return {
        "name": "Fixture Holder",
        "line1": "1 Fixture Street",
        "city": "Portland",
        "state": "OR",
        "postal_code": "97201",
        "country": "US",
    }


class StandaloneTests(unittest.TestCase):
    def test_generated_address_endpoint_normalizes_each_account_address(self):
        payload = {
            "code": "200",
            "data": {
                "firstname": "Test",
                "lastname": "Holder",
                "address": {
                    "street": "1 Fixture Street",
                    "city": "Wilmington",
                    "zipcode": "19801",
                    "country_code": "US",
                    "area_code": "DE",
                },
            },
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch("standalone_flow.urlopen", return_value=Response()):
            result = fetch_billing_address(
                {"publishable_key": "pk_test_fixture"}
            )
            live_result = fetch_billing_address(
                {"publishable_key": "pk_live_fixture"}
            )
        self.assertEqual(result["source"], "address_api")
        self.assertEqual(live_result["source"], "address_api")
        self.assertEqual(result["billing"]["state"], "DE")
        self.assertEqual(result["billing"]["postal_code"], "19801")

    def test_task_steps_show_live_stage_and_failure_location(self):
        task_id = "step_fixture"
        TASKS.pop(task_id, None)
        _set_task(task_id, steps=_initial_steps("full"), stage="准备执行")
        _update_step(task_id, "first_link", "running", "正在首次提链")
        self.assertIn("首次提链", TASKS[task_id]["stage"])
        _update_step(task_id, "first_link", "done", "首次提链完成")
        _update_step(task_id, "bind", "running", "正在执行绑卡")
        _fail_running_step(task_id, "银行卡被拒绝")
        bind = next(step for step in TASKS[task_id]["steps"] if step["key"] == "bind")
        self.assertEqual(bind["status"], "error")
        self.assertIn("银行卡被拒绝", bind["detail"])
        TASKS.pop(task_id, None)
        flow_source = (ROOT / "standalone_flow.py").read_text(encoding="utf-8-sig")
        payment_source = (ROOT / "standalone_core" / "card_payment.py").read_text(encoding="utf-8-sig")
        for marker in (
            "FLOW_STEP:fingerprint",
            "FLOW_STEP:first_link",
            "FLOW_STEP:second_link",
            "FLOW_STEP:payment",
            "FLOW_STEP:verify",
        ):
            self.assertIn(marker, flow_source + payment_source)

    def test_preflight_checkout_is_reused_by_first_account_run(self):
        preflight_payload = {
            "access_token": fixture_token(),
            "bind_proxy_pool": ["http://bind-proxy:8080"],
            "promo_proxy_pool": ["http://link-proxy:8081"],
        }
        checkout = {
            "ok": True,
            "checkout_id": "oaics_cached_fixture",
            "processor_entity": "openai_llc",
            "_publishable_key": "pk_fixture",
            "amount": "0",
            "url": "https://chatgpt.com/checkout/openai_llc/oaics_cached_fixture",
        }
        payment_result = {
            "ok": True,
            "checkout_id": "oaics_cached_fixture",
            "processor_entity": "openai_llc",
        }
        with patch("standalone_flow._extract_checkout", return_value=checkout) as extract:
            with patch(
                "standalone_flow.fetch_stripe_publishable_key",
                return_value="pk_test_fixture",
            ):
                with patch("standalone_flow.run_card_payment", return_value=payment_result):
                    preflight(preflight_payload)
                    run_flow({
                        **preflight_payload,
                        "payment_method_id": "pm_fixture",
                        "payment_method_publishable_key": "pk_test_fixture",
                        "card_last4": "4242",
                        "flow_mode": "bind_only",
                        "billing": fixture_billing(),
                    })
        self.assertEqual(extract.call_count, 1)

    def test_preflight_rotates_to_next_bind_proxy_candidate(self):
        checkout = {
            "ok": True,
            "checkout_id": "oaics_proxy_fixture",
            "processor_entity": "openai_llc",
            "_publishable_key": "pk_fixture",
            "amount": "0",
            "url": "https://chatgpt.com/checkout/openai_llc/oaics_proxy_fixture",
        }
        with (
            patch("standalone_flow._extract_checkout", return_value=checkout),
            patch("standalone_flow._remember_preflight"),
            patch(
                "standalone_flow.fetch_stripe_publishable_key",
                side_effect=[
                    RuntimeError("proxy connect failed"),
                    RuntimeError("proxy TLS connect failed"),
                    "pk_test_fixture",
                ],
            ) as fetch_key,
        ):
            result = preflight(
                {
                    "access_token": fixture_token(),
                    "bind_proxy_pool": [
                        "http://bind-one:8080",
                        "http://bind-two:8080",
                    ],
                    "promo_proxy_pool": ["http://link-proxy:8081"],
                }
            )
        self.assertEqual(result["publishable_key"], "pk_test_fixture")
        self.assertEqual(fetch_key.call_count, 3)
        self.assertEqual(fetch_key.call_args_list[0].kwargs["proxy"], "http://bind-one:8080")
        self.assertEqual(fetch_key.call_args_list[1].kwargs["proxy"], "https://bind-one:8080")
        self.assertEqual(fetch_key.call_args_list[2].kwargs["proxy"], "http://bind-two:8080")

    def test_speed_optimizations_are_enabled(self):
        frontend = (ROOT / "static" / "direct-bind.js").read_text(encoding="utf-8-sig")
        flow = (ROOT / "standalone_flow.py").read_text(encoding="utf-8-sig")
        extractor = (ROOT / "standalone_core" / "ph_shortlink_extractor.py").read_text(encoding="utf-8-sig")
        payment = (ROOT / "standalone_core" / "card_payment.py").read_text(encoding="utf-8-sig")
        config_path = ROOT / "standalone_config.json"
        if not config_path.exists():
            config_path = ROOT / "standalone_config.example.json"
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        self.assertIn("ACCOUNT_SAVE_DEBOUNCE_MS", frontend)
        self.assertIn("allocateFingerprints", frontend)
        self.assertIn("String(account.fingerprintRegion || '').toUpperCase() !== 'US'", frontend)
        self.assertIn("_take_preflight(ctx)", flow)
        self.assertIn("stage1_session if candidate == stage1_proxy", extractor)
        self.assertIn("if not config.fast_verify", payment)
        self.assertIn('config.get("fast_verify", False)', flow)
        self.assertFalse(config["fast_verify"])
        self.assertIn("allow_redisplay: 'always'", frontend)

    def test_multiple_proxy_rows_are_parsed_and_round_robin_assigned(self):
        page = (ROOT / "index.html").read_text(encoding="utf-8-sig")
        frontend = (ROOT / "static" / "direct-bind.js").read_text(encoding="utf-8-sig")
        self.assertIn("function parseProxyPool", frontend)
        self.assertIn("slice(0, 500)", frontend)
        self.assertIn("bindPool[(bindStart + index) % bindPool.length]", frontend)
        self.assertIn("promoPool[(promoStart + index) % promoPool.length]", frontend)
        self.assertIn("MAX_PROXY_FALLBACKS = 4", frontend)
        self.assertIn("account.bindProxyPool", frontend)
        self.assertIn("account.promoProxyPool", frontend)
        self.assertIn("bind[accountIndex % bind.length]", frontend)
        self.assertIn("const previewBindPool = data.bind", frontend)
        self.assertIn("const previewPromoPool = data.promo", frontend)
        self.assertIn("bind_proxy_pool: previewBindPool", frontend)
        self.assertIn("promo_proxy_pool: previewPromoPool", frontend)
        self.assertIn("HTTP、HTTPS、SOCKS4、SOCKS4A、SOCKS5、SOCKS5H", page)
        self.assertIn("<small>优惠地区节点</small>", page)

    def test_fingerprint_is_allocated_locally_when_account_is_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "fingerprint_profiles.json"
            with patch("standalone_core.fingerprint_store.STORE_PATH", store):
                result = allocate_fingerprint({"access_token": fixture_token()})
        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], "account_fixture")
        self.assertEqual(len(result["fingerprint_id"]), 16)
        self.assertIn(result["tls"], {"chrome145", "chrome146"})
        self.assertEqual(result["region"], "US")

    def test_unsupported_chrome144_transport_falls_back_to_chrome142(self):
        self.assertEqual(
            choose_tls_impersonate(
                browser="chrome", chrome_major=144, platform_os="Windows"
            ),
            "chrome142",
        )

    def test_fingerprint_batch_uses_one_store_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "fingerprint_profiles.json"
            with patch("standalone_core.fingerprint_store.STORE_PATH", store):
                with patch(
                    "standalone_core.fingerprint_store._save_store",
                    wraps=__import__(
                        "standalone_core.fingerprint_store", fromlist=["_save_store"]
                    )._save_store,
                ) as save:
                    result = allocate_fingerprints({
                        "accounts": [
                            {"client_id": "one", "access_token": fixture_token()},
                            {
                                "client_id": "two",
                                "access_token": fixture_token().replace(
                                    "account_fixture", "account_fixture_two"
                                ),
                            },
                        ]
                    })
        self.assertEqual(result["count"], 2)
        self.assertEqual(save.call_count, 1)
        self.assertTrue(all(item["ok"] for item in result["items"]))

    def test_existing_tr_fingerprint_is_migrated_to_us(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "fingerprint_profiles.json"
            with patch("standalone_core.fingerprint_store.STORE_PATH", store):
                tr_profile, device_id = get_account_fingerprint(
                    "region_migration_fixture", region="TR"
                )
                us_profile, migrated_device_id = get_account_fingerprint(
                    "region_migration_fixture", region="US"
                )
        self.assertEqual(tr_profile.region, "TR")
        self.assertEqual(us_profile.region, "US")
        self.assertTrue(us_profile.timezone_id.startswith("America/"))
        self.assertEqual(device_id, migrated_device_id)

    def test_store_optimizer_upgrades_obsolete_chrome144_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "fingerprint_profiles.json"
            with patch("standalone_core.fingerprint_store.STORE_PATH", store):
                from standalone_core.register_profile import random_profile
                from standalone_core import fingerprint_store

                profile = random_profile(region="US", chrome_major=144)
                device_id = "6f19e012-3140-48ff-b500-34b6442fa374"
                payload = {
                    "version": 2,
                    "accounts": {
                        "fixture": {
                            "device_id": device_id,
                            "fingerprint_id": "old",
                            "profile": profile_to_json(profile),
                        }
                    },
                }
                store.write_text(json.dumps(payload), encoding="utf-8")
                fingerprint_store._STORE_CACHE = None
                result = optimize_fingerprint_store("US")
                migrated = json.loads(store.read_text(encoding="utf-8"))
                restored = next(iter(migrated["accounts"].values()))
                upgraded = fingerprint_store.profile_from_json(restored["profile"])
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(upgraded.browser_major, 146)
        self.assertEqual(upgraded.tls_impersonate, "chrome146")

    def test_registration_fingerprint_is_sticky_and_shared_with_payment(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "fingerprint_profiles.json"
            with patch("standalone_core.fingerprint_store.STORE_PATH", store):
                first, first_device = get_account_fingerprint(
                    "account_fixture", region="PH", proxy="http://proxy-region-ph:8080"
                )
                second, second_device = get_account_fingerprint(
                    "account_fixture", region="PH", proxy="http://another-proxy:8080"
                )
                get_account_fingerprint(
                    "account_fixture_2", region="PH", proxy="http://proxy-region-ph:8080"
                )
                store.write_text("{broken", encoding="utf-8")
                recovered, recovered_device = get_account_fingerprint(
                    "account_fixture", region="PH", proxy="http://proxy-region-ph:8080"
                )
            self.assertEqual(profile_to_json(first), profile_to_json(second))
            self.assertEqual(profile_to_json(first), profile_to_json(recovered))
            self.assertEqual(first_device, second_device)
            self.assertEqual(first_device, recovered_device)
            self.assertTrue(first.device_name)
            self.assertTrue(first.mac_address)
            audit = audit_fingerprint(first, first_device)
            self.assertTrue(audit["ok"])
            adapted = extractor_profile(first, first_device)
            self.assertEqual(adapted["ua"], first.user_agent)
            self.assertEqual(adapted["tls_impersonate"], first.tls_impersonate)
            self.assertEqual(adapted["fingerprint_id"], audit["fingerprint_id"])

            class CookieJar:
                def __init__(self):
                    self.values = {}

                def set(self, *_args, **_kwargs):
                    if len(_args) >= 2:
                        self.values[_args[0]] = _args[1]
                    return None

            class Session:
                def __init__(self):
                    self.headers = {}
                    self.cookies = CookieJar()
                    self.trust_env = True

            session = Session()
            with patch.object(card_payment, "curl_requests") as curl:
                curl.Session.return_value = session
                created = card_payment._new_session(
                    card_payment.CardPaymentConfig(
                        token="fixture",
                        device_id=first_device,
                        fingerprint_profile=adapted,
                        locale=first.language,
                        timezone=first.timezone_id,
                    ),
                    proxy="http://bind-proxy:8080",
                )
            curl.Session.assert_called_once_with(impersonate=first.tls_impersonate)
            self.assertIs(created, session)
            self.assertEqual(created.headers["User-Agent"], first.user_agent)
            self.assertEqual(created.headers["oai-device-id"], first_device)
            self.assertEqual(created.headers["sec-ch-ua"], first.sec_ch_ua)
            self.assertEqual(created.headers["sec-ch-ua-model"], '""')
            self.assertEqual(created.headers["priority"], "u=1, i")
            self.assertNotIn("Cookie", created.headers)
            self.assertEqual(created.cookies.values["oai-did"], first_device)

            link_session = Session()
            built, profile_name = build_session(
                "fixture",
                "http://link-proxy:8081",
                session_factory=lambda: link_session,
                fingerprint_profile=adapted,
                device_id=first_device,
            )
            self.assertIs(built, link_session)
            self.assertEqual(profile_name, adapted["name"])
            self.assertEqual(built.headers["User-Agent"], created.headers["User-Agent"])
            self.assertEqual(built.headers["sec-ch-ua"], created.headers["sec-ch-ua"])
            self.assertEqual(built.headers["oai-device-id"], first_device)
            self.assertEqual(built.cookies.values["oai-did"], first_device)

    def test_payload_uses_token_directly_without_project_database(self):
        data = validate_payload(
            {
                "access_token": fixture_token(),
                "payment_method_id": "pm_fixture",
                "card_last4": "4242",
                "flow_mode": "full",
                "bind_proxy_pool": ["127.0.0.1:8080"],
                "promo_proxy_pool": ["127.0.0.2:8080"],
                "billing": fixture_billing(),
                "sentinel_token": "sentinel_fixture",
                "telemetry": "telemetry_fixture",
            }
        )
        self.assertEqual(data["account_id"], "account_fixture")
        self.assertEqual(data["email"], "fixture@example.test")
        self.assertEqual(data["payment_method_id"], "pm_fixture")
        self.assertEqual(data["billing"]["country"], "US")
        self.assertEqual(data["sentinel_token"], "sentinel_fixture")
        self.assertEqual(data["telemetry"], "telemetry_fixture")

    def test_billing_address_is_explicit_and_validated(self):
        payload = {
            "access_token": fixture_token(),
            "payment_method_id": "pm_fixture",
            "flow_mode": "full",
            "bind_proxy_pool": ["127.0.0.1:8080"],
            "promo_proxy_pool": ["127.0.0.2:8080"],
            "billing": {**fixture_billing(), "postal_code": "bad"},
        }
        with self.assertRaisesRegex(ValueError, "ZIP"):
            validate_payload(payload)
        page = (ROOT / "index.html").read_text(encoding="utf-8-sig")
        frontend = (ROOT / "static" / "direct-bind.js").read_text(encoding="utf-8-sig")
        self.assertNotIn("billingLine1", page)
        self.assertNotIn("billingFields", page)
        self.assertIn("billingAutoStatus", page)
        self.assertNotIn("account-billing-grid", frontend)
        self.assertIn("fetchBillingForAccount", frontend)
        self.assertIn("assignBillingAddresses", frontend)
        self.assertIn("billingMode: account.billingMode", frontend)
        self.assertIn("billing: account.billing", frontend)
        self.assertIn("normalizedBilling(account?.billing", frontend)
        self.assertIn("billing_details", frontend)
        self.assertIn("billing: browserCard.billing", frontend)
        self.assertIn("/api/address", frontend)
        self.assertNotIn("pk_test_", frontend)
        self.assertIn("ensureStripeJs", frontend)
        self.assertIn("https://js.stripe.com/v3/", frontend)
        self.assertNotIn('<script src="https://js.stripe.com/v3/">', page)

    def test_service_has_no_5550_or_project_database_reference(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8-sig")
        frontend = (ROOT / "static" / "direct-bind.js").read_text(encoding="utf-8-sig")
        for forbidden in ("127.0.0.1:5550", "PROJECT_APP_URL", "console.db", "import db", "/api/project-flow"):
            self.assertNotIn(forbidden, source + frontend)
        self.assertIn('/api/fingerprint/allocate', source)
        self.assertIn('/api/address', source)

    def test_proxy_protocols_are_preserved_and_supported(self):
        data = validate_payload(
            {
                "access_token": fixture_token(),
                "payment_method_id": "pm_fixture",
                "flow_mode": "full",
                "bind_proxy_pool": ["https://user:pass@127.0.0.1:8080"],
                "promo_proxy_pool": ["socks5://user:pass@127.0.0.2:1080"],
                "billing": fixture_billing(),
            }
        )
        self.assertEqual(
            data["bind_pool"], [
                "https://user:pass@127.0.0.1:8080",
                "http://user:pass@127.0.0.1:8080",
            ]
        )
        self.assertEqual(
            data["promo_pool"], [
                "socks5://user:pass@127.0.0.2:1080",
                "socks5h://user:pass@127.0.0.2:1080",
            ]
        )
        self.assertEqual(normalize_proxy("socks4://host:1080"), "socks4://host:1080")
        self.assertEqual(normalize_proxy("socks4a://host:1080"), "socks4a://host:1080")
        self.assertEqual(normalize_proxy("socks5h://host:1080"), "socks5h://host:1080")
        self.assertEqual(normalize_proxy("host:8080"), "http://host:8080")
        self.assertEqual(
            normalize_proxy("host:8080:user:pass"),
            "http://user:pass@host:8080",
        )
        with self.assertRaisesRegex(ValueError, "代理协议不受支持"):
            normalize_proxy("ftp://host:21")

    def test_proxy_pool_roles_are_isolated(self):
        context = {
            "access_token": fixture_token(),
            "bind_pool": ["http://bind-proxy:8080"],
            "promo_pool": ["http://link-proxy:8081"],
        }
        with patch("standalone_flow.Mode8Extractor") as extractor:
            extractor.return_value.run.return_value = {
                "ok": True,
                "checkout_id": "oaics_fixture",
                "_publishable_key": "pk_fixture",
            }
            _extract_checkout(context, {}, lambda _message: None)
        extractor.return_value.run.assert_called_once_with(
            "http://link-proxy:8081", "http://link-proxy:8081"
        )
        source = (ROOT / "standalone_flow.py").read_text(encoding="utf-8-sig")
        self.assertIn('bind_candidates = list(ctx["bind_pool"][:8])', source)

    def test_checkout_rotates_proxy_after_transport_failure(self):
        context = {
            "access_token": fixture_token(),
            "promo_pool": ["https://link-proxy:8081", "http://link-proxy:8081"],
        }
        success = {
            "ok": True,
            "checkout_id": "oaics_fixture",
            "_publishable_key": "pk_fixture",
        }
        with patch("standalone_flow.Mode8Extractor") as extractor:
            extractor.return_value.run.side_effect = [
                RuntimeError("curl: (35) TLS connect error"),
                success,
            ]
            result = _extract_checkout(context, {}, lambda _message: None)
        self.assertEqual(result["checkout_id"], "oaics_fixture")
        self.assertEqual(extractor.return_value.run.call_count, 2)
        self.assertEqual(
            extractor.return_value.run.call_args_list[0].args,
            ("https://link-proxy:8081", "https://link-proxy:8081"),
        )
        self.assertEqual(
            extractor.return_value.run.call_args_list[1].args,
            ("http://link-proxy:8081", "http://link-proxy:8081"),
        )

    def test_card_flow_rotates_bind_proxy_after_transport_failure(self):
        checkout = {
            "ok": True,
            "checkout_id": "oaics_fixture",
            "processor_entity": "openai_llc",
            "_publishable_key": "pk_fixture",
            "url": "https://chatgpt.com/checkout/openai_llc/oaics_fixture",
        }
        payment = {
            "ok": True,
            "checkout_id": "oaics_fixture",
            "processor_entity": "openai_llc",
            "card_last4": "4242",
        }
        with (
            patch("standalone_flow._attach_fingerprint"),
            patch("standalone_flow._take_preflight", return_value=checkout),
            patch(
                "standalone_flow.run_card_payment",
                side_effect=[RuntimeError("proxy connect failed"), payment],
            ) as card_payment,
        ):
            result = run_flow(
                {
                    "access_token": fixture_token(),
                    "payment_method_id": "pm_fixture",
                    "card_last4": "4242",
                    "flow_mode": "bind_only",
                    "bind_proxy_pool": ["https://bind-proxy:8080"],
                    "promo_proxy_pool": ["http://link-proxy:8081"],
                    "billing": fixture_billing(),
                }
            )
        self.assertTrue(result["ok"])
        self.assertEqual(card_payment.call_count, 2)
        self.assertEqual(card_payment.call_args_list[0].kwargs["proxy"], "https://bind-proxy:8080")
        self.assertEqual(card_payment.call_args_list[1].kwargs["proxy"], "http://bind-proxy:8080")

    def test_card_flow_does_not_replay_after_remote_mutation_started(self):
        checkout = {
            "ok": True,
            "checkout_id": "oaics_fixture",
            "processor_entity": "openai_llc",
            "_publishable_key": "pk_fixture",
            "url": "https://chatgpt.com/checkout/openai_llc/oaics_fixture",
        }
        ambiguous_failure = card_payment.CardPaymentError(
            "post-mutation transport failure: proxy TLS reset",
            proxy_retry_safe=False,
        )
        logs: list[str] = []
        with (
            patch("standalone_flow._attach_fingerprint"),
            patch("standalone_flow._take_preflight", return_value=checkout),
            patch(
                "standalone_flow.run_card_payment",
                side_effect=ambiguous_failure,
            ) as payment,
        ):
            with self.assertRaises(card_payment.CardPaymentError) as raised:
                run_flow(
                    {
                        "access_token": fixture_token(),
                        "payment_method_id": "pm_fixture",
                        "card_last4": "4242",
                        "flow_mode": "bind_only",
                        "bind_proxy_pool": [
                            "https://bind-proxy:8080",
                            "http://bind-proxy:8080",
                        ],
                        "promo_proxy_pool": ["http://link-proxy:8081"],
                        "billing": fixture_billing(),
                    },
                    logger=logs.append,
                )
        self.assertIs(raised.exception, ambiguous_failure)
        self.assertEqual(payment.call_count, 1)
        self.assertTrue(any("避免重复扣款" in message for message in logs))

    def test_errors_are_translated_to_chinese(self):
        self.assertIn("代理 TLS 握手失败", _safe_error("curl: (35) TLS connect error"))
        self.assertIn("账号没有新增支付方式", _safe_error("HTTP 402 card_declined Your card was declined"))
        self.assertIn("缺少账号 ID", _safe_error("AT does not contain an account id"))
        self.assertIn(
            "订阅尚未提交：缺少当前 Checkout 会话生成的动态校验数据",
            _safe_error(
                "checkout confirm precondition: missing flow-bound "
                "OpenAI-Sentinel-Token, OAI-Telemetry"
            ),
        )

    def test_checkout_confirmation_waiting_state_is_rendered(self):
        source = (ROOT / "static" / "direct-bind.js").read_text(encoding="utf-8-sig")
        self.assertIn("task.status === 'action_required'", source)
        self.assertIn("'action_required'].includes(item.status)", source)
        self.assertIn("return '待提交'", source)
        self.assertIn("订阅尚未提交", source)
        self.assertIn("个尚未提交", source)
        self.assertIn("function canonicalCheckoutLink", source)
        self.assertIn("function restoredAccountStage", source)
        self.assertIn("lastTaskId: taskId", source)
        self.assertIn("订阅待确认：使用当前行的 Checkout 链接继续", source)
        self.assertIn("旧任务未保存 Checkout 链接", source)
        self.assertEqual(
            _checkout_link(
                {
                    "checkout_id": "oaics_fixture",
                    "processor_entity": "openai_llc",
                }
            ),
            "https://chatgpt.com/checkout/openai_llc/oaics_fixture",
        )

    def test_action_required_task_rebuilds_and_persists_checkout_link(self):
        task_id = "checkout-link-recovery-fixture"
        TASKS.pop(task_id, None)
        with patch(
            "server.run_flow",
            return_value={
                "ok": False,
                "action_required": "checkout_session_confirmation",
                "checkout_id": "oaics_fixture",
                "processor_entity": "openai_llc",
                "checkout_link": "",
            },
        ):
            _run_task(task_id, {})
        task = TASKS.pop(task_id)
        self.assertEqual(task["status"], "action_required")
        self.assertEqual(
            task["result"]["checkout_link"],
            "https://chatgpt.com/checkout/openai_llc/oaics_fixture",
        )
        self.assertIn("使用当前行的 Checkout 链接继续", task["stage"])
        self.assertIn(
            "订阅请求已提交，账号风控返回 status=blocked",
            _safe_error("checkout confirm: server returned blocked"),
        )
        self.assertIn(
            "自动重试已关闭",
            _safe_error("checkout confirm: server returned blocked"),
        )

    def test_account_import_is_memory_only_and_list_driven(self):
        source = (ROOT / "static" / "direct-bind.js").read_text(encoding="utf-8-sig")
        page = (ROOT / "index.html").read_text(encoding="utf-8-sig")
        self.assertNotIn("sessionStorage", source)
        self.assertNotIn("direct-bind:at-list", source)
        self.assertIn("direct-bind:account-list:v2", source)
        self.assertIn("direct-bind:proxy-draft:v5", source)
        self.assertIn("localStorage.removeItem(key)", source)
        self.assertIn("saveAccounts", source)
        self.assertIn("loadAccounts", source)
        self.assertIn("accountImportInput", page)
        self.assertIn("selectAllAccounts", page)
        self.assertIn("deleteSelectedButton", page)
        self.assertIn("/api/fingerprint/allocate", source)
        self.assertIn("bind_proxy_pool: bind.length ? [bind[accountIndex % bind.length]]", source)
        self.assertIn("hydrateMissingFingerprints", source)
        self.assertIn("fingerprintId", source)
        self.assertNotIn("account-step-list", source)
        self.assertIn("account-row__current-step", source)
        css_source = (ROOT / "static" / "direct-bind.css").read_text(encoding="utf-8")
        self.assertIn("overflow-wrap:anywhere", css_source)
        self.assertIn("task.steps", source)
        allocation = source.index("assignProxies(accounts, data.bind, data.promo, mode)")
        self.assertIn("const settled = await submitAlignedWave(prepared, mode);", source[allocation:])
        self.assertIn("/api/standalone-flow/quick-checkout-batch", source)
        self.assertIn("Promise.allSettled", source)
        self.assertIn("start_delay_ms: 250", source)
        self.assertIn('max="50"', page)
        self.assertIn("MAX_BATCH_CONCURRENCY = 50", source)
        self.assertIn("namedAccessTokens", source)
        self.assertIn("decoded.accountId || decoded.userId || decoded.email", source)
        self.assertIn("userId: String(auth.user_id", source)
        self.assertIn("accountImportStatus", page)
        self.assertIn("/api/account/resolve", source)
        self.assertIn("resolveAccountIds", source)
        self.assertNotIn(".filter((token) => Boolean(decodeAccount(token).accountId))", source)
        self.assertIn("state.accounts.length !== items.length", source)
        self.assertIn("account.stage = '环境已就绪'", source)
        self.assertIn("replace(/^注册指纹已就绪$/, '环境已就绪')", source)

    def test_account_id_is_selected_from_account_check_ordering(self):
        payload = {
            "accounts": {
                "account_first": {
                    "account": {"account_id": "account_first"},
                    "can_access_with_session": True,
                },
                "default": {
                    "account": {"account_id": "account_first"},
                    "can_access_with_session": True,
                },
            },
            "account_ordering": ["account_first"],
        }
        self.assertEqual(_select_account_id(payload), "account_first")

    def test_aligned_batch_barrier_releases_threads_together(self):
        called: list[float] = []
        called_lock = threading.Lock()

        def fake_run_flow(_payload, logger):
            del logger
            with called_lock:
                called.append(time.perf_counter())
            return {"ok": True}

        task_ids = [f"aligned-{index}" for index in range(4)]
        start_at = time.perf_counter() + 0.15
        with patch("server.run_flow", side_effect=fake_run_flow):
            threads = [
                threading.Thread(
                    target=_run_task,
                    args=(task_id, {"flow_mode": "bind_only"}, start_at, "fixture-group"),
                )
                for task_id in task_ids
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
        self.assertEqual(len(called), len(task_ids))
        self.assertLess((max(called) - min(called)) * 1000, 50)
        for task_id in task_ids:
            self.assertEqual(TASKS[task_id]["start_group"], "fixture-group")
            TASKS.pop(task_id, None)

    def test_flow_buttons_match_requested_order_and_modes(self):
        page = (ROOT / "index.html").read_text(encoding="utf-8-sig")
        source = (ROOT / "static" / "direct-bind.js").read_text(encoding="utf-8-sig")
        first = page.index("1 提链 + 绑卡 + 提链 + 支付")
        second = page.index("2 提链 + 绑卡")
        third = page.index("3 提链 + 支付")
        fourth = page.index("4 提链")
        self.assertLess(first, second)
        self.assertLess(second, third)
        self.assertLess(third, fourth)
        self.assertIn("runBatch('full', '提链 + 绑卡 + 提链 + 支付')", source)
        self.assertIn("runBatch('bind_only', '提链 + 绑卡')", source)
        self.assertIn("runBatch('link_pay', '提链 + 支付')", source)
        self.assertIn("runBatch('link_only', '提链')", source)

    def test_link_only_requires_only_promo_proxy_and_no_card(self):
        data = validate_payload(
            {
                "access_token": fixture_token(),
                "flow_mode": "link_only",
                "promo_proxy_pool": ["127.0.0.2:8080"],
            }
        )
        self.assertEqual(data["bind_pool"], [])
        self.assertEqual(data["payment_method_id"], "")
        self.assertEqual(
            data["promo_pool"],
            ["http://127.0.0.2:8080", "https://127.0.0.2:8080"],
        )
        self.assertEqual(
            [step["key"] for step in _initial_steps("link_only")],
            ["proxy", "fingerprint", "first_link"],
        )

    def test_link_only_returns_after_checkout_extraction(self):
        checkout = {
            "ok": True,
            "checkout_id": "oaics_fixture",
            "processor_entity": "openai_llc",
            "_publishable_key": "pk_fixture",
            "url": "https://chatgpt.com/checkout/openai_llc/oaics_fixture",
        }
        with (
            patch("standalone_flow._attach_fingerprint"),
            patch("standalone_flow._take_preflight", return_value=None),
            patch("standalone_flow._extract_checkout", return_value=checkout) as extract,
            patch("standalone_flow.run_card_payment") as payment,
        ):
            result = run_flow(
                {
                    "access_token": fixture_token(),
                    "flow_mode": "link_only",
                    "promo_proxy_pool": ["127.0.0.2:8080"],
                }
            )
        extract.assert_called_once()
        payment.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["payment_flow"], "standalone-http-link-only")
        self.assertEqual(result["checkout_link"], checkout["url"])

    def test_footer_uses_single_batch_summary_instead_of_log_history(self):
        page = (ROOT / "index.html").read_text(encoding="utf-8-sig")
        source = (ROOT / "static" / "direct-bind.js").read_text(encoding="utf-8-sig")
        style = (ROOT / "static" / "direct-bind.css").read_text(encoding="utf-8-sig")
        self.assertNotIn('id="status"', page)
        self.assertIn('id="batchSummary"', page)
        self.assertIn("function showBatchSummary", source)
        self.assertIn("批量支付", source)
        self.assertIn("function copyText", source)
        self.assertIn("link.textContent = '复制链接'", source)
        self.assertNotIn("link.textContent = '打开链接'", source)
        self.assertIn('id="exportCsvButton"', page)
        self.assertIn("导出 CSV", page)
        self.assertNotIn("复制全部链接", page)
        self.assertIn("function exportAccountsCsv", source)
        self.assertIn("text/csv;charset=utf-8", source)
        self.assertIn(".export-csv-button", style)
        self.assertIn("grid-template-columns:minmax(360px,32%) minmax(300px,1fr) auto", style)
        self.assertIn(".batch-summary", style)
        self.assertIn("void message;", source)
        self.assertIn("errorText.startsWith(stageLabel)", source)

    def test_health_and_info_are_standalone(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            health = json.loads(urlopen(base + "/health", timeout=3).read())
            info = json.loads(urlopen(base + "/api/standalone/info", timeout=3).read())
            page_response = urlopen(base + "/", timeout=3)
            page_response.read()
            self.assertTrue(health["ok"])
            self.assertEqual(health["service"], "direct-bind-standalone")
            self.assertFalse(info["upstream_app"])
            self.assertFalse(info["project_database"])
            self.assertEqual(info["fingerprint_provider"], "registration-profile")
            self.assertTrue(info["fingerprint_sticky_per_account"])
            self.assertEqual(info["fingerprint_store_version"], 3)
            self.assertEqual(info["fingerprint_batch_limit"], 500)
            self.assertEqual(info["aligned_batch_limit"], 50)
            self.assertIn("no-store", page_response.headers.get("Cache-Control", ""))
            launcher = (ROOT / "serve_direct.py").read_text(encoding="utf-8-sig")
            self.assertIn('optimize_fingerprint_store("US")', launcher)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
