from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from standalone_core.card_payment import (
    CardPaymentConfig,
    CardPaymentError,
    run_card_payment,
)
from standalone_core.ph_shortlink_extractor import Mode8Config, Mode8Extractor


class FakeResponse:
    def __init__(
        self,
        payload=None,
        status_code: int = 200,
        raw_text: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.text = raw_text if raw_text is not None else json.dumps(self.payload)
        self.headers = dict(headers or {})

    def json(self):
        return self.payload


class PaymentContractSession:
    """Offline response fixture for the captured bind -> pay API contract."""

    def __init__(
        self,
        *,
        final_status: str = "succeeded",
        plan_type: str = "plus",
        intent_type: str = "setup_intent",
        expected_checkout_id: str = "oaics_second_fixture",
        conditional_offer: bool = False,
        app_payment_method_visible: bool = True,
        payment_method_sequence: list[bool] | None = None,
        plan_sequence: list[str] | None = None,
        subscription_http_sequence: list[int] | None = None,
        consistency_updates: bool = False,
        checkout_confirm_status: str = "",
    ):
        self.final_status = final_status
        self.plan_type = plan_type
        self.intent_type = intent_type
        self.expected_checkout_id = expected_checkout_id
        self.conditional_offer = conditional_offer
        self.app_payment_method_visible = app_payment_method_visible
        self.payment_method_sequence = list(
            payment_method_sequence
            if payment_method_sequence is not None
            else [app_payment_method_visible]
        )
        self.payment_method_calls = 0
        self.plan_sequence = list(plan_sequence or [plan_type])
        self.subscription_http_sequence = list(
            subscription_http_sequence or [200]
        )
        self.consistency_updates = consistency_updates
        self.checkout_confirm_status = checkout_confirm_status
        self.subscription_calls = 0
        self.checkout_confirm_count = 0
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        self.calls: list[tuple[str, str]] = []
        self.request_headers: dict[tuple[str, str], dict[str, str]] = {}
        self.stripe_request_headers: list[dict[str, str]] = []
        self.checkout_confirm_body = None
        self.checkout_confirm_headers = None
        self.confirmation_token_form = None
        self.final_intent_form = None
        self.elements_session_params = None
        self.taxes_body = None
        self.success_data_params = None
        self.verify_page_params = None
        self.auth_session_params = None
        self.auth_session_calls: list[dict[str, str]] = []
        self.subscription_authorizations: list[str] = []
        self.closed = False

    def _record(self, method: str, url: str) -> str:
        path = urlsplit(url).path
        self.calls.append((method, path))
        return path

    def _effective_headers(self, request_headers) -> dict[str, str]:
        effective = dict(self.headers)
        for key, value in (request_headers or {}).items():
            if value is None:
                effective.pop(key, None)
            else:
                effective[key] = value
        return effective

    def get(self, url, **kwargs):
        path = self._record("GET", url)
        effective_headers = self._effective_headers(kwargs.get("headers"))
        self.request_headers[("GET", path)] = effective_headers
        if urlsplit(url).netloc == "api.stripe.com":
            self.stripe_request_headers.append(effective_headers)
        if path == "/":
            return FakeResponse(
                {},
                raw_text=(
                    '<html data-build="prod-fixture" data-seq="8848688">'
                    '<script>{"webDeploymentAttestation":"att_fixture",'
                    '"sessionId":"11111111-2222-3333-4444-555555555555"}</script>'
                    "</html>"
                ),
            )
        if path == "/backend-api/payments/stripe_client_bootstrap":
            return FakeResponse({"publishable_key": "pk_test_fixture"})
        if path.startswith("/backend-api/payments/checkout/"):
            return FakeResponse(
                {
                    "customer_session_client_secret": "cuss_fixture_secret",
                    "custom_payment_methods": ["cpmt_fixture"],
                    "payment_method_types": ["link", "card"],
                }
            )
        if path == "/v1/elements/sessions":
            self.elements_session_params = kwargs.get("params") or {}
            return FakeResponse(
                {
                    "id": "elements_session_fixture",
                    "elements_session_config_id": "config_fixture",
                }
            )
        if path == "/v1/setup_intents/seti_preflight_fixture":
            return FakeResponse(
                {"id": "seti_preflight_fixture", "status": "succeeded"}
            )
        if path == "/backend-api/payments/payment_methods":
            index = min(
                self.payment_method_calls, len(self.payment_method_sequence) - 1
            )
            self.payment_method_calls += 1
            return FakeResponse(
                {
                    "data": (
                        [{"id": "pm_browser_fixture"}]
                        if self.payment_method_sequence[index]
                        else []
                    )
                }
            )
        if path == "/v1/payment_methods":
            return FakeResponse({"data": [{"id": "pm_browser_fixture"}]})
        if path == "/backend-api/subscriptions":
            index = min(self.subscription_calls, len(self.plan_sequence) - 1)
            http_index = min(
                self.subscription_calls,
                len(self.subscription_http_sequence) - 1,
            )
            self.subscription_calls += 1
            self.subscription_authorizations.append(
                effective_headers.get("Authorization", "")
            )
            headers = (
                {"x-oai-is-pending-updates-ack": "3:00000001"}
                if self.consistency_updates
                else None
            )
            return FakeResponse(
                {"plan_type": self.plan_sequence[index]},
                status_code=self.subscription_http_sequence[http_index],
                headers=headers,
            )
        if path == "/checkout/verify":
            self.verify_page_params = kwargs.get("params") or {}
            return FakeResponse({"ok": True})
        if path == "/payments/success.data":
            self.success_data_params = kwargs.get("params") or {}
            return FakeResponse({"ok": True})
        if path == "/api/auth/session":
            self.auth_session_params = kwargs.get("params") or {}
            self.auth_session_calls.append(dict(self.auth_session_params))
            token = (
                "fixture-refreshed-token"
                if self.auth_session_params.get("refresh") == "true"
                else "fixture-workspace-token"
            )
            return FakeResponse({"accessToken": token})
        return FakeResponse({"ok": True})

    def post(self, url, *, data=None, json=None, **kwargs):
        path = self._record("POST", url)
        effective_headers = self._effective_headers(kwargs.get("headers"))
        self.request_headers[("POST", path)] = effective_headers
        if urlsplit(url).netloc == "api.stripe.com":
            self.stripe_request_headers.append(effective_headers)
        if path == "/backend-api/payments/payment_method":
            return FakeResponse(
                {
                    "id": "seti_bind_fixture",
                    "client_secret": "seti_bind_fixture_secret_value",
                }
            )
        if path == "/v1/setup_intents/seti_bind_fixture/confirm":
            form = parse_qs(data or "")
            if form.get("payment_method") != ["pm_browser_fixture"]:
                return FakeResponse({"error": {"message": "wrong payment method"}}, 400)
            return FakeResponse(
                {
                    "id": "seti_bind_fixture",
                    "status": "succeeded",
                    "payment_method": "pm_browser_fixture",
                    "customer": "cus_fixture",
                }
            )
        if path == "/backend-api/payments/checkout/taxes":
            self.taxes_body = json
            return FakeResponse({"ok": True})
        if path == "/v1/confirmation_tokens":
            self.confirmation_token_form = parse_qs(data or "")
            return FakeResponse({"id": "ctoken_fixture"})
        if path == "/backend-api/payments/checkout/confirm":
            self.checkout_confirm_count += 1
            self.checkout_confirm_body = json
            self.checkout_confirm_headers = effective_headers
            required = {
                "checkout_session_id": self.expected_checkout_id,
                "confirm_token": "ctoken_fixture",
                "selected_payment_method_type": "card",
            }
            if json != required:
                if self.conditional_offer and self.checkout_confirm_count == 2 and json == {
                    "checkout_session_id": self.expected_checkout_id
                }:
                    return FakeResponse(
                        {
                            "id": "pi_final_fixture",
                            "client_secret": "pi_final_fixture_secret_value",
                            "type": "payment_intent",
                            "status": "requires_confirmation",
                            "conditional_offer_preflight": True,
                        }
                    )
                return FakeResponse({"error": {"message": "wrong confirm body"}}, 422)
            if self.conditional_offer and self.checkout_confirm_count == 1:
                return FakeResponse(
                    {
                        "id": "seti_preflight_fixture",
                        "client_secret": "seti_preflight_fixture_secret_value",
                        "type": "setup_intent",
                        "status": "succeeded",
                        "conditional_offer_preflight": True,
                    }
                )
            if self.checkout_confirm_status:
                return FakeResponse({"status": self.checkout_confirm_status})
            prefix = "pi" if self.intent_type == "payment_intent" else "seti"
            return FakeResponse(
                {
                    "id": f"{prefix}_final_fixture",
                    "client_secret": f"{prefix}_final_fixture_secret_value",
                    "type": self.intent_type,
                    "status": "requires_confirmation",
                },
                headers=(
                    {"x-oai-is-update": "ois_fixture_update"}
                    if self.consistency_updates
                    else None
                ),
            )
        if path == "/v1/setup_intents/seti_final_fixture/confirm":
            self.final_intent_form = parse_qs(data or "")
            return FakeResponse(
                {"id": "seti_final_fixture", "status": self.final_status}
            )
        if path == "/v1/payment_intents/pi_final_fixture/confirm":
            self.final_intent_form = parse_qs(data or "")
            return FakeResponse(
                {"id": "pi_final_fixture", "status": self.final_status}
            )
        return FakeResponse({"ok": True})

    def close(self):
        self.closed = True


class ShortlinkContractSession:
    def __init__(self):
        self.headers: dict[str, str] = {}
        self.checkout_headers: dict[str, str] = {}

    def get(self, url, **_kwargs):
        return FakeResponse(
            {},
            raw_text=(
                '<html data-build="prod-link-fixture" data-seq="8848689">'
                '<script>{"webDeploymentAttestation":"att_link_fixture",'
                '"sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}</script>'
                "</html>"
            ),
        )

    def post(self, _url, **kwargs):
        self.checkout_headers = {
            key: value for key, value in (kwargs.get("headers") or {}).items()
        }
        return FakeResponse(
            {
                "checkout_session_id": "oaics_link_fixture",
                "processor_entity": "openai_ie",
            }
        )


def config() -> CardPaymentConfig:
    return CardPaymentConfig(
        token="fixture-token",
        account_id="account_fixture",
        country="PH",
        currency="PHP",
        billing={
            "name": "Fixture Holder",
            "line1": "1 Fixture Street",
            "city": "Wilmington",
            "state": "DE",
            "postal_code": "19801",
            "country": "US",
        },
        payment_method_id="pm_browser_fixture",
        payment_method_publishable_key="pk_test_fixture",
        card_last4="4242",
        checkout_id="oaics_first_fixture",
        processor_entity="openai_llc",
        publishable_key="pk_test_fixture",
        strong_bind_direct=True,
        fast_verify=True,
        payment_method_poll_delays=(0.0, 0.0, 0.0),
        subscription_poll_delays=(0.0, 0.0, 0.0),
        sentinel_token="sentinel_fixture",
        telemetry="telemetry_fixture",
    )


def refreshed_checkout():
    return {
        "checkout_session_id": "oaics_second_fixture",
        "processor_entity": "openai_llc",
        "currency": "PHP",
        "amount": "0",
        "_publishable_key": "pk_test_fixture",
    }


class PaymentContractTests(unittest.TestCase):
    def test_checkout_create_uses_separate_flow_token_and_bootstrap_metadata(self):
        session = ShortlinkContractSession()
        extractor = Mode8Extractor(
            Mode8Config(
                token="fixture-token",
                checkout_sentinel_token="checkout_sentinel_fixture",
                checkout_telemetry="checkout_telemetry_fixture",
            ),
            logger=lambda _message: None,
        )
        metadata = extractor._hydrate_payment_metadata(session)
        payload = extractor._create_checkout(session)

        self.assertEqual(payload["checkout_session_id"], "oaics_link_fixture")
        self.assertEqual(metadata["client_build_number"], "8848689")
        self.assertEqual(session.headers["OAI-Client-Version"], "prod-link-fixture")
        self.assertNotIn("OAI-Web-Deployment-Attestation", session.headers)
        self.assertEqual(
            session.checkout_headers["OAI-Web-Deployment-Attestation"],
            "att_link_fixture",
        )
        self.assertEqual(
            session.checkout_headers["OpenAI-Sentinel-Token"],
            "checkout_sentinel_fixture",
        )
        self.assertEqual(
            session.checkout_headers["OAI-Telemetry"],
            "checkout_telemetry_fixture",
        )

    def test_full_payment_contract_succeeds_offline_without_a_card(self):
        session = PaymentContractSession()
        result = run_card_payment(
            config(),
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=refreshed_checkout,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["setup_status"], "succeeded")
        self.assertEqual(result["intent_type"], "setup_intent")
        self.assertEqual(result["subscription_plan"], "plus")
        self.assertTrue(session.closed)
        self.assertIn(
            ("POST", "/backend-api/payments/checkout/confirm"), session.calls
        )
        self.assertIn(
            ("POST", "/v1/setup_intents/seti_final_fixture/confirm"), session.calls
        )
        self.assertEqual(
            session.confirmation_token_form["client_context[payment_method_types][0]"],
            ["link"],
        )
        self.assertEqual(
            session.confirmation_token_form["client_context[payment_method_types][1]"],
            ["card"],
        )
        self.assertEqual(
            session.confirmation_token_form["set_as_default_payment_method"],
            ["false"],
        )
        self.assertEqual(
            session.confirmation_token_form[
                "client_attribution_metadata[payment_method_selection_flow]"
            ],
            ["merchant_specified"],
        )
        self.assertEqual(
            session.elements_session_params["deferred_intent[amount]"], "0"
        )
        self.assertEqual(
            session.elements_session_params["customer_session_client_secret"],
            "cuss_fixture_secret",
        )
        self.assertEqual(
            session.elements_session_params["custom_payment_methods[0]"],
            "cpmt_fixture",
        )
        self.assertEqual(
            session.confirmation_token_form[
                "client_attribution_metadata[elements_session_id]"
            ],
            ["elements_session_fixture"],
        )
        self.assertEqual(
            session.confirmation_token_form[
                "client_attribution_metadata[elements_session_config_id]"
            ],
            ["config_fixture"],
        )
        self.assertNotIn("use_stripe_sdk", session.final_intent_form)
        self.assertEqual(
            session.checkout_confirm_headers.get("OAI-Client-Build-Number"),
            "8848688",
        )
        self.assertEqual(
            session.checkout_confirm_headers.get("OAI-Client-Version"),
            "prod-fixture",
        )
        self.assertEqual(
            session.checkout_confirm_headers.get("OAI-Session-Id"),
            "11111111-2222-3333-4444-555555555555",
        )
        self.assertEqual(
            session.checkout_confirm_headers.get("OAI-Web-Deployment-Attestation"),
            "att_fixture",
        )
        subscription_headers = session.request_headers[
            ("GET", "/backend-api/subscriptions")
        ]
        self.assertNotIn("OAI-Web-Deployment-Attestation", subscription_headers)
        for stripe_headers in session.stripe_request_headers:
            self.assertNotIn("Authorization", stripe_headers)
            self.assertNotIn("OAI-Client-Build-Number", stripe_headers)
            self.assertNotIn("OAI-Client-Version", stripe_headers)
            self.assertNotIn("OAI-Session-Id", stripe_headers)
            self.assertNotIn("OAI-Web-Deployment-Attestation", stripe_headers)
        payment_method_list_headers = session.request_headers[
            ("GET", "/v1/payment_methods")
        ]
        self.assertIn("checkout_server_update_beta=v1", payment_method_list_headers["Stripe-Version"])
        self.assertEqual(
            session.taxes_body,
            {
                "checkout_session_id": "oaics_second_fixture",
                "checkout_email": "",
                "billing_country": "US",
                "billing_name": "Fixture Holder",
                "currency": "php",
                "processor_entity": "openai_llc",
                "billing_address": {
                    "line1": "1 Fixture Street",
                    "city": "Wilmington",
                    "state": "DE",
                    "postal_code": "19801",
                    "country": "US",
                },
            },
        )

    def test_success_loader_uses_captured_route_parameters(self):
        session = PaymentContractSession()
        payment_config = config()
        payment_config.fast_verify = False
        result = run_card_payment(
            payment_config,
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=refreshed_checkout,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            session.success_data_params,
            {
                "stripe_session_id": "oaics_second_fixture",
                "plan_type": "plus",
                "processor_entity": "openai_llc",
                "_routes": "routes/payments.success",
            },
        )
        self.assertEqual(
            session.verify_page_params,
            {
                "stripe_session_id": "oaics_second_fixture",
                "processor_entity": "openai_llc",
                "plan_type": "plus",
                "redirect_status": "succeeded",
                "setup_intent": "seti_final_fixture",
                "setup_intent_client_secret": (
                    "seti_final_fixture_secret_value"
                ),
            },
        )
        self.assertEqual(
            session.auth_session_params,
            {
                "workspace_update": "true",
                "reason": "checkout_success",
                "path": "/payments/success",
            },
        )
        verify_headers = session.request_headers[("GET", "/checkout/verify")]
        self.assertNotIn("Content-Type", verify_headers)
        self.assertTrue(verify_headers["Accept"].startswith("text/html"))
        success_headers = session.request_headers[
            ("GET", "/payments/success.data")
        ]
        self.assertIn("setup_intent=seti_final_fixture", success_headers["Referer"])
        auth_headers = session.request_headers[("GET", "/api/auth/session")]
        self.assertEqual(auth_headers["x-openai-target-path"], "/api/auth/session")
        self.assertEqual(auth_headers["x-openai-target-route"], "/api/auth/session")
        self.assertNotIn("Content-Type", auth_headers)
        final_confirm = session.calls.index(
            ("POST", "/v1/setup_intents/seti_final_fixture/confirm")
        )
        self.assertEqual(
            session.calls[final_confirm + 1 : final_confirm + 4],
            [
                ("GET", "/checkout/verify"),
                (
                    "GET",
                    "/backend-api/payments/checkout/openai_llc/oaics_second_fixture",
                ),
                ("GET", "/payments/success.data"),
            ],
        )

    def test_subscription_activation_lag_is_polled(self):
        session = PaymentContractSession(plan_sequence=["", "", "plus"])
        result = run_card_payment(
            config(),
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=refreshed_checkout,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(session.subscription_calls, 3)

    def test_workspace_auth_token_is_adopted_before_subscription_check(self):
        session = PaymentContractSession()
        session.headers["Authorization"] = "Bearer fixture-stale-token"
        payment_config = config()
        payment_config.fast_verify = False
        result = run_card_payment(
            payment_config,
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=refreshed_checkout,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            session.subscription_authorizations,
            ["Bearer fixture-workspace-token"],
        )

    def test_subscription_401_refreshes_auth_once_and_retries_only_get(self):
        session = PaymentContractSession(
            subscription_http_sequence=[401, 200]
        )
        session.headers["Authorization"] = "Bearer fixture-stale-token"
        payment_config = config()
        payment_config.fast_verify = False
        result = run_card_payment(
            payment_config,
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=refreshed_checkout,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(session.subscription_calls, 2)
        self.assertEqual(
            session.auth_session_calls[-1],
            {
                "refresh": "true",
                "reason": "token_expired",
                "method": "GET",
                "path": "/backend-api/subscriptions",
            },
        )
        self.assertEqual(
            session.subscription_authorizations,
            [
                "Bearer fixture-workspace-token",
                "Bearer fixture-refreshed-token",
            ],
        )
        self.assertEqual(result["auth_refresh_status"], 200)
        self.assertEqual(
            session.calls.count(
                ("POST", "/backend-api/payments/checkout/confirm")
            ),
            1,
        )
        self.assertEqual(
            session.calls.count(
                ("POST", "/v1/setup_intents/seti_final_fixture/confirm")
            ),
            1,
        )

    def test_oai_consistency_update_reaches_subscription_verification(self):
        session = PaymentContractSession(consistency_updates=True)
        result = run_card_payment(
            config(),
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=refreshed_checkout,
        )
        self.assertTrue(result["ok"])
        pending = session.request_headers[
            ("GET", "/backend-api/subscriptions")
        ]["x-oai-is-pending-updates"]
        self.assertEqual(
            json.loads(pending),
            {"v": 3, "updates": ["ois_fixture_update"]},
        )
        self.assertEqual(getattr(session, "_oai_pending_updates", []), [])

    def test_checkout_confirm_uses_har_js_body_and_payment_intent_branch(self):
        session = PaymentContractSession(intent_type="payment_intent")
        payment_config = config()
        payment_config.telemetry = ""
        payment_config.fast_verify = False
        result = run_card_payment(
            payment_config,
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=refreshed_checkout,
        )
        self.assertEqual(result["intent_type"], "payment_intent")
        self.assertEqual(
            session.checkout_confirm_body,
            {
                "checkout_session_id": "oaics_second_fixture",
                "confirm_token": "ctoken_fixture",
                "selected_payment_method_type": "card",
            },
        )
        self.assertIn(
            ("POST", "/v1/payment_intents/pi_final_fixture/confirm"), session.calls
        )
        self.assertEqual(
            session.checkout_confirm_headers.get("OpenAI-Sentinel-Token"),
            "sentinel_fixture",
        )
        self.assertEqual(
            session.checkout_confirm_headers.get("OAI-Telemetry"),
            "[1,null]",
        )
        self.assertEqual(
            session.verify_page_params["payment_intent"], "pi_final_fixture"
        )
        self.assertEqual(
            session.verify_page_params["payment_intent_client_secret"],
            "pi_final_fixture_secret_value",
        )

    def test_checkout_confirm_blocked_is_account_risk_and_never_replayed(self):
        session = PaymentContractSession(checkout_confirm_status="blocked")
        with self.assertRaises(CardPaymentError) as raised:
            run_card_payment(
                config(),
                session_factory=lambda _config, _proxy: session,
                refresh_checkout=refreshed_checkout,
            )
        self.assertIn("account risk control", str(raised.exception))
        self.assertFalse(raised.exception.proxy_retry_safe)
        self.assertEqual(session.checkout_confirm_count, 1)
        self.assertFalse(
            any(
                method == "POST" and path.endswith("/confirm")
                for method, path in session.calls
                if path.startswith("/v1/payment_intents/")
                or path.startswith("/v1/setup_intents/seti_final_")
            )
        )

    def test_conditional_offer_continues_after_succeeded_preflight(self):
        session = PaymentContractSession(conditional_offer=True)
        result = run_card_payment(
            config(),
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=refreshed_checkout,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["intent_type"], "payment_intent")
        self.assertEqual(session.checkout_confirm_count, 2)
        self.assertIn(
            ("GET", "/v1/setup_intents/seti_preflight_fixture"), session.calls
        )
        self.assertIn(
            ("POST", "/v1/payment_intents/pi_final_fixture/confirm"),
            session.calls,
        )
        self.assertNotIn("confirmation_token", session.final_intent_form)

    def test_link_pay_skips_bind_and_second_checkout(self):
        session = PaymentContractSession(
            intent_type="setup_intent",
            expected_checkout_id="oaics_first_fixture",
        )
        payment_config = config()
        payment_config.flow_mode = "link_pay"

        def unexpected_refresh():
            raise AssertionError("link_pay must not create a second checkout")

        result = run_card_payment(
            payment_config,
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=unexpected_refresh,
        )
        self.assertTrue(result["ok"])
        self.assertNotIn(
            ("POST", "/backend-api/payments/payment_method"), session.calls
        )
        self.assertNotIn(
            ("POST", "/v1/setup_intents/seti_bind_fixture/confirm"), session.calls
        )
        self.assertIn(
            ("POST", "/backend-api/payments/checkout/confirm"), session.calls
        )

    def test_post_mutation_disconnect_is_marked_non_replayable(self):
        class DisconnectAfterSubmitSession(PaymentContractSession):
            def post(self, url, *, data=None, json=None, **kwargs):
                if urlsplit(url).path == "/backend-api/payments/checkout/confirm":
                    self._record("POST", url)
                    raise ConnectionError("proxy TLS reset after request submit")
                return super().post(url, data=data, json=json, **kwargs)

        session = DisconnectAfterSubmitSession(
            expected_checkout_id="oaics_first_fixture"
        )
        payment_config = config()
        payment_config.flow_mode = "link_pay"
        with self.assertRaises(CardPaymentError) as raised:
            run_card_payment(
                payment_config,
                session_factory=lambda _config, _proxy: session,
            )
        self.assertIn("post-mutation transport failure", str(raised.exception))
        self.assertFalse(raised.exception.proxy_retry_safe)
        self.assertTrue(session.closed)

    def test_missing_checkout_session_context_preserves_actionable_link(self):
        session = PaymentContractSession()
        payment_config = config()
        payment_config.sentinel_token = ""
        with self.assertRaises(CardPaymentError) as raised:
            run_card_payment(
                payment_config,
                session_factory=lambda _config, _proxy: session,
                refresh_checkout=refreshed_checkout,
            )
        error = raised.exception
        self.assertEqual(error.action_required, "checkout_session_confirmation")
        self.assertEqual(error.checkout_id, "oaics_second_fixture")
        self.assertEqual(error.processor_entity, "openai_llc")
        self.assertEqual(
            error.checkout_link,
            "https://chatgpt.com/checkout/openai_llc/oaics_second_fixture",
        )
        self.assertFalse(error.proxy_retry_safe)
        self.assertNotIn(
            ("POST", "/backend-api/payments/checkout/confirm"), session.calls
        )

    def test_link_pay_rejects_checkout_from_another_stripe_merchant(self):
        session = PaymentContractSession(expected_checkout_id="oaics_first_fixture")
        payment_config = config()
        payment_config.flow_mode = "link_pay"
        payment_config.publishable_key = "pk_test_other_checkout_merchant"
        with self.assertRaisesRegex(
            CardPaymentError, "direct payment merchant shard mismatch"
        ):
            run_card_payment(
                payment_config,
                session_factory=lambda _config, _proxy: session,
            )
        self.assertNotIn(
            ("POST", "/v1/confirmation_tokens"), session.calls
        )

    def test_http_200_requires_action_is_not_reported_as_paid(self):
        session = PaymentContractSession(final_status="requires_action")
        with self.assertRaisesRegex(CardPaymentError, "requires_action"):
            run_card_payment(
                config(),
                session_factory=lambda _config, _proxy: session,
                refresh_checkout=refreshed_checkout,
            )

    def test_http_200_with_missing_plan_is_not_reported_as_paid(self):
        session = PaymentContractSession(plan_type="")
        with self.assertRaisesRegex(CardPaymentError, "Plus plan was not activated"):
            run_card_payment(
                config(),
                session_factory=lambda _config, _proxy: session,
                refresh_checkout=refreshed_checkout,
            )

    def test_bound_card_visibility_lag_is_polled(self):
        session = PaymentContractSession(
            payment_method_sequence=[False, False, True]
        )
        result = run_card_payment(
            config(),
            session_factory=lambda _config, _proxy: session,
            refresh_checkout=refreshed_checkout,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(session.payment_method_calls, 3)

    def test_http_200_without_bound_card_in_account_list_is_rejected(self):
        session = PaymentContractSession(app_payment_method_visible=False)
        with self.assertRaisesRegex(CardPaymentError, "not visible on account"):
            run_card_payment(
                config(),
                session_factory=lambda _config, _proxy: session,
                refresh_checkout=refreshed_checkout,
            )

    def test_browser_payment_method_must_match_account_stripe_merchant(self):
        session = PaymentContractSession()
        test_config = config()
        test_config.payment_method_publishable_key = "pk_test_other_merchant"
        with self.assertRaisesRegex(CardPaymentError, "merchant shard mismatch"):
            run_card_payment(
                test_config,
                session_factory=lambda _config, _proxy: session,
                refresh_checkout=refreshed_checkout,
            )
        self.assertNotIn(
            ("POST", "/backend-api/payments/payment_method"), session.calls
        )


if __name__ == "__main__":
    unittest.main()
