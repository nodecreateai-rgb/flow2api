import asyncio
import importlib.util
import json
import sys
import types
import unittest
import tomllib
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("tomli", tomllib)


def load_generation_handler():
    src_pkg = sys.modules.setdefault("src", types.ModuleType("src"))
    src_pkg.__path__ = [str(ROOT / "src")]

    services_pkg = sys.modules.setdefault("src.services", types.ModuleType("src.services"))
    services_pkg.__path__ = [str(ROOT / "src" / "services")]

    core_pkg = sys.modules.setdefault("src.core", types.ModuleType("src.core"))
    core_pkg.__path__ = [str(ROOT / "src" / "core")]

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
        cache_timeout = 7200
        cache_enabled = False
        cache_base_url = ""
        server_host = "127.0.0.1"
        server_port = 8000
        flow_image_request_timeout = 180
        upsample_timeout = 300
        debug_enabled = False

    config_mod.config = DummyConfig()
    sys.modules["src.core.config"] = config_mod

    models_mod = types.ModuleType("src.core.models")

    class RequestLog:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    models_mod.Task = type("Task", (), {})
    models_mod.RequestLog = RequestLog
    sys.modules["src.core.models"] = models_mod

    account_tiers_mod = types.ModuleType("src.core.account_tiers")
    account_tiers_mod.PAYGATE_TIER_NOT_PAID = "PAYGATE_TIER_NOT_PAID"

    def normalize_user_paygate_tier(user_paygate_tier):
        return user_paygate_tier or "PAYGATE_TIER_NOT_PAID"

    def get_paygate_tier_label(user_paygate_tier):
        mapping = {
            "PAYGATE_TIER_NOT_PAID": "Normal",
            "PAYGATE_TIER_ONE": "Pro",
            "PAYGATE_TIER_TWO": "Ult",
        }
        return mapping.get(user_paygate_tier, "Normal")

    def get_required_paygate_tier_for_model(model_name):
        normalized = (model_name or "").lower()
        if normalized.endswith("-4k"):
            return "PAYGATE_TIER_TWO"
        if normalized.endswith("-2k"):
            return "PAYGATE_TIER_ONE"
        return "PAYGATE_TIER_NOT_PAID"

    def supports_model_for_tier(model_name, user_paygate_tier):
        required = get_required_paygate_tier_for_model(model_name)
        ranks = {
            "PAYGATE_TIER_NOT_PAID": 0,
            "PAYGATE_TIER_ONE": 1,
            "PAYGATE_TIER_TWO": 2,
        }
        return ranks.get(normalize_user_paygate_tier(user_paygate_tier), 0) >= ranks.get(required, 0)

    account_tiers_mod.normalize_user_paygate_tier = normalize_user_paygate_tier
    account_tiers_mod.get_paygate_tier_label = get_paygate_tier_label
    account_tiers_mod.get_required_paygate_tier_for_model = get_required_paygate_tier_for_model
    account_tiers_mod.supports_model_for_tier = supports_model_for_tier
    sys.modules["src.core.account_tiers"] = account_tiers_mod

    file_cache_mod = types.ModuleType("src.services.file_cache")

    class FileCache:
        def __init__(self, cache_dir, default_timeout, proxy_manager=None):
            self.cache_dir = cache_dir
            self.default_timeout = default_timeout
            self.proxy_manager = proxy_manager

        async def cache_base64_image(self, encoded_image, resolution):
            return f"{resolution.lower()}.jpg"

    file_cache_mod.FileCache = FileCache
    sys.modules["src.services.file_cache"] = file_cache_mod

    module_name = "src.services.generation_handler"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "src" / "services" / "generation_handler.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.GenerationHandler


GenerationHandler = load_generation_handler()


class FakeDB:
    def __init__(self):
        self.request_logs = []
        self.request_log_updates = []

    async def add_request_log(self, log):
        self.request_logs.append(log)
        return len(self.request_logs)

    async def update_request_log(self, log_id, **kwargs):
        self.request_log_updates.append({"id": log_id, **kwargs})


class FakeTokenManager:
    def __init__(self, token):
        self.token = token
        self.recorded_errors = []
        self.recorded_usage = []
        self.recorded_success = []

    async def ensure_valid_token(self, token):
        return token

    async def ensure_project_exists(self, token_id):
        return "project-123"

    async def record_usage(self, token_id, is_video=False):
        self.recorded_usage.append((token_id, is_video))

    async def record_success(self, token_id):
        self.recorded_success.append(token_id)

    async def record_error(self, token_id):
        self.recorded_errors.append(token_id)


class FakeLoadBalancer:
    def __init__(self, token):
        self.token = token

    async def select_token(self, **kwargs):
        return self.token

    async def release_pending(self, token_id, **kwargs):
        return


class FakeFlowClient:
    def __init__(
        self,
        *,
        generate_delay=0.0,
        generate_exception=None,
        upsample_delay=0.0,
        upsample_exception=None,
        upsample_result="ZmFrZS11cHNjYWxlZA==",
    ):
        self.generate_delay = generate_delay
        self.generate_exception = generate_exception
        self.upsample_delay = upsample_delay
        self.upsample_exception = upsample_exception
        self.upsample_result = upsample_result
        self.generate_calls = 0
        self.upsample_calls = 0
        self.last_upsample_resolution = None
        self.last_generate_budget = None
        self.last_upsample_budget = None

    def clear_request_fingerprint(self):
        return

    async def upload_image(self, at, image_bytes, aspect_ratio, project_id=None):
        return "uploaded-media"

    async def generate_image(
        self,
        at,
        project_id,
        prompt,
        model_name,
        aspect_ratio,
        image_inputs=None,
        token_id=None,
        token_image_concurrency=None,
        max_total_wait_seconds=None,
    ):
        self.generate_calls += 1
        self.last_generate_budget = max_total_wait_seconds
        await asyncio.sleep(self.generate_delay)
        if self.generate_exception is not None:
            raise self.generate_exception
        return (
            {
                "media": [
                    {
                        "name": "media-001",
                        "image": {
                            "generatedImage": {
                                "fifeUrl": "https://example.com/generated.png",
                            }
                        },
                    }
                ]
            },
            ";session-001",
            {"generation_attempts": [{"launch_queue_ms": 0, "launch_stagger_ms": 0}]},
        )

    async def upsample_image(
        self,
        at,
        project_id,
        media_id,
        target_resolution,
        user_paygate_tier,
        session_id=None,
        token_id=None,
        max_total_wait_seconds=None,
    ):
        self.upsample_calls += 1
        self.last_upsample_resolution = target_resolution
        self.last_upsample_budget = max_total_wait_seconds
        await asyncio.sleep(self.upsample_delay)
        if self.upsample_exception is not None:
            raise self.upsample_exception
        return self.upsample_result

    def _get_retry_reason(self, error_str):
        return None

    def _is_timeout_error(self, error):
        return False


def build_token(user_paygate_tier):
    return SimpleNamespace(
        id=1,
        email="tester@example.com",
        at="at-token",
        image_concurrency=1,
        video_concurrency=1,
        user_paygate_tier=user_paygate_tier,
    )


class GenerationHandlerWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def _collect_chunks(self, handler, *, model, stream=False):
        chunks = []
        async for chunk in handler.handle_generation(
            model=model,
            prompt="draw a lighthouse",
            images=None,
            stream=stream,
        ):
            chunks.append(chunk)
        return chunks

    def _build_handler(self, flow_client, user_paygate_tier):
        token = build_token(user_paygate_tier)
        db = FakeDB()
        token_manager = FakeTokenManager(token)
        load_balancer = FakeLoadBalancer(token)
        handler = GenerationHandler(
            flow_client=flow_client,
            token_manager=token_manager,
            load_balancer=load_balancer,
            db=db,
            concurrency_manager=None,
            proxy_manager=None,
        )
        return handler, db, token_manager

    async def test_normal_image_model_returns_direct_url(self):
        handler, db, token_manager = self._build_handler(
            FakeFlowClient(),
            user_paygate_tier="PAYGATE_TIER_NOT_PAID",
        )

        chunks = await self._collect_chunks(
            handler,
            model="gemini-2.5-flash-image-landscape",
            stream=False,
        )
        payload = json.loads(chunks[-1])
        content = payload["choices"][0]["message"]["content"]

        self.assertIn("https://example.com/generated.png", content)
        self.assertEqual(token_manager.recorded_errors, [])
        self.assertEqual(len(db.request_log_updates), 0)
        self.assertIsNotNone(handler.flow_client.last_generate_budget)

    async def test_2k_image_model_returns_upscaled_payload(self):
        flow_client = FakeFlowClient(upsample_result="ZmFrZS0yaw==")
        handler, _, token_manager = self._build_handler(
            flow_client,
            user_paygate_tier="PAYGATE_TIER_ONE",
        )

        chunks = await self._collect_chunks(
            handler,
            model="gemini-3.0-pro-image-landscape-2k",
            stream=False,
        )
        payload = json.loads(chunks[-1])
        content = payload["choices"][0]["message"]["content"]

        self.assertIn("data:image/jpeg;base64,ZmFrZS0yaw==", content)
        self.assertEqual(flow_client.generate_calls, 1)
        self.assertEqual(flow_client.upsample_calls, 1)
        self.assertEqual(flow_client.last_upsample_resolution, "UPSAMPLE_IMAGE_RESOLUTION_2K")
        self.assertIsNotNone(flow_client.last_generate_budget)
        self.assertIsNotNone(flow_client.last_upsample_budget)
        self.assertEqual(token_manager.recorded_errors, [])

    async def test_4k_image_model_returns_upscaled_payload(self):
        flow_client = FakeFlowClient(upsample_result="ZmFrZS00aw==")
        handler, _, token_manager = self._build_handler(
            flow_client,
            user_paygate_tier="PAYGATE_TIER_TWO",
        )

        chunks = await self._collect_chunks(
            handler,
            model="gemini-3.1-flash-image-landscape-4k",
            stream=False,
        )
        payload = json.loads(chunks[-1])
        content = payload["choices"][0]["message"]["content"]

        self.assertIn("data:image/jpeg;base64,ZmFrZS00aw==", content)
        self.assertEqual(flow_client.generate_calls, 1)
        self.assertEqual(flow_client.upsample_calls, 1)
        self.assertEqual(flow_client.last_upsample_resolution, "UPSAMPLE_IMAGE_RESOLUTION_4K")
        self.assertIsNotNone(flow_client.last_generate_budget)
        self.assertIsNotNone(flow_client.last_upsample_budget)
        self.assertEqual(token_manager.recorded_errors, [])

    async def test_submit_watchdog_times_out_and_progress_advances(self):
        flow_client = FakeFlowClient(generate_delay=0.35)
        handler, db, token_manager = self._build_handler(
            flow_client,
            user_paygate_tier="PAYGATE_TIER_TWO",
        )
        handler._resolve_image_submit_timeout = lambda model_config: 0.12
        handler._resolve_image_stage_heartbeat_seconds = lambda upsample=False: 0.05

        chunks = await self._collect_chunks(
            handler,
            model="gemini-3.0-pro-image-landscape-4k",
            stream=True,
        )
        error_chunks = [chunk for chunk in chunks if isinstance(chunk, str) and chunk.strip().startswith("{")]
        payload = json.loads(error_chunks[-1])
        progress_updates = [
            update
            for update in db.request_log_updates
            if update.get("status_text") == "submitting_image"
        ]

        self.assertTrue(progress_updates)
        self.assertGreater(max(update.get("progress", 0) for update in progress_updates), 28)
        self.assertIn("图片提交超时", payload["error"]["message"])
        self.assertEqual(flow_client.generate_calls, 1)
        self.assertEqual(flow_client.upsample_calls, 0)
        self.assertTrue(token_manager.recorded_errors)


if __name__ == "__main__":
    unittest.main()
