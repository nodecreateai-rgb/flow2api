import asyncio
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_flow_client():
    src_pkg = sys.modules.setdefault("src", types.ModuleType("src"))
    src_pkg.__path__ = [str(ROOT / "src")]

    services_pkg = sys.modules.setdefault("src.services", types.ModuleType("src.services"))
    services_pkg.__path__ = [str(ROOT / "src" / "services")]

    core_pkg = sys.modules.setdefault("src.core", types.ModuleType("src.core"))
    core_pkg.__path__ = [str(ROOT / "src" / "core")]

    curl_cffi_pkg = types.ModuleType("curl_cffi")
    curl_requests_mod = types.ModuleType("curl_cffi.requests")

    class AsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    curl_requests_mod.AsyncSession = AsyncSession
    curl_cffi_pkg.requests = curl_requests_mod
    sys.modules["curl_cffi"] = curl_cffi_pkg
    sys.modules["curl_cffi.requests"] = curl_requests_mod

    logger_mod = types.ModuleType("src.core.logger")

    class DummyLogger:
        def log_info(self, *args, **kwargs):
            return

        def log_warning(self, *args, **kwargs):
            return

        def log_error(self, *args, **kwargs):
            return

        def log_request(self, *args, **kwargs):
            return

        def log_response(self, *args, **kwargs):
            return

    logger_mod.debug_logger = DummyLogger()
    sys.modules["src.core.logger"] = logger_mod

    config_mod = types.ModuleType("src.core.config")

    class DummyConfig:
        flow_labs_base_url = "https://labs.google/fx/api"
        flow_api_base_url = "https://aisandbox-pa.googleapis.com/v1"
        flow_timeout = 120
        flow_max_retries = 3
        flow_image_request_timeout = 180
        flow_image_timeout_retry_count = 1
        flow_image_timeout_retry_delay = 0.8
        flow_image_timeout_use_media_proxy_fallback = False
        flow_image_prefer_media_proxy = False
        upsample_timeout = 300
        debug_enabled = False
        captcha_method = "disabled"
        remote_browser_base_url = ""
        remote_browser_api_key = ""
        remote_browser_timeout = 60

    config_mod.config = DummyConfig()
    sys.modules["src.core.config"] = config_mod

    module_name = "src.services.flow_client"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "src" / "services" / "flow_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.FlowClient


FlowClient = load_flow_client()


class FlowClientBudgetTests(unittest.TestCase):
    def test_timeout_budget_caps_request_without_rounding_up(self):
        client = FlowClient(None)

        timeout_seconds = client._resolve_request_timeout_with_budget(
            base_timeout=180,
            operation_label="budget-check",
            started_at=time.time(),
            max_total_wait_seconds=5.9,
        )

        self.assertEqual(timeout_seconds, 5)

    def test_timeout_budget_exhaustion_fails_fast(self):
        client = FlowClient(None)

        with self.assertRaises(TimeoutError):
            client._resolve_request_timeout_with_budget(
                base_timeout=180,
                operation_label="budget-check",
                started_at=time.time(),
                max_total_wait_seconds=4.9,
            )

    def test_generate_image_passes_total_budget_to_http_layer(self):
        client = FlowClient(None)
        captured = {}

        async def fake_get_recaptcha_token(project_id, action="IMAGE_GENERATION", token_id=None):
            return "captcha-token", "browser-1"

        async def fake_make_image_generation_request(**kwargs):
            captured["max_total_wait_seconds"] = kwargs.get("max_total_wait_seconds")
            return {"media": []}

        async def fake_notify(browser_id=None):
            return

        client._get_recaptcha_token = fake_get_recaptcha_token
        client._make_image_generation_request = fake_make_image_generation_request
        client._notify_browser_captcha_request_finished = fake_notify

        async def run():
            result, session_id, trace = await client.generate_image(
                at="at-token",
                project_id="project-123",
                prompt="draw a castle",
                model_name="GEM_PIX_2",
                aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
                max_total_wait_seconds=12.5,
            )
            return result, session_id, trace

        result, session_id, trace = asyncio.run(run())

        self.assertEqual(result, {"media": []})
        self.assertTrue(session_id.startswith(";"))
        self.assertEqual(trace["final_success_attempt"], 1)
        self.assertAlmostEqual(captured["max_total_wait_seconds"], 12.5, places=2)

    def test_upsample_image_uses_budget_capped_timeout(self):
        client = FlowClient(None)
        captured = {}

        async def fake_get_recaptcha_token(project_id, action="IMAGE_GENERATION", token_id=None):
            return "captcha-token", "browser-1"

        async def fake_make_request(**kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return {"encodedImage": "ZmFrZS11cHNjYWxlZA=="}

        async def fake_notify(browser_id=None):
            return

        client._get_recaptcha_token = fake_get_recaptcha_token
        client._make_request = fake_make_request
        client._notify_browser_captcha_request_finished = fake_notify

        async def run():
            return await client.upsample_image(
                at="at-token",
                project_id="project-123",
                media_id="media-1",
                target_resolution="UPSAMPLE_IMAGE_RESOLUTION_4K",
                user_paygate_tier="PAYGATE_TIER_TWO",
                max_total_wait_seconds=5.9,
            )

        encoded = asyncio.run(run())

        self.assertEqual(encoded, "ZmFrZS11cHNjYWxlZA==")
        self.assertEqual(captured["timeout"], 5)

    def test_extract_upsample_encoded_image_supports_nested_fallback_fields(self):
        client = FlowClient(None)
        fake_base64 = "ZmFrZS11cHNjYWxlZA==" * 16

        encoded, source = client._extract_upsample_encoded_image(
            {
                "image": {
                    "base64": fake_base64,
                }
            }
        )

        self.assertEqual(encoded, fake_base64)
        self.assertEqual(source, "root.image.base64")

    def test_extract_upsample_encoded_image_raises_when_payload_missing(self):
        client = FlowClient(None)

        with self.assertRaises(ValueError):
            client._extract_upsample_encoded_image({"status": "ok", "image": {}})

    def test_extract_upsample_encoded_image_ignores_short_non_image_payload(self):
        client = FlowClient(None)

        with self.assertRaises(ValueError):
            client._extract_upsample_encoded_image({"data": "ok"})


if __name__ == "__main__":
    unittest.main()
