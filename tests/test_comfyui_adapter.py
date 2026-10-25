"""Tests for the real ComfyUI job flow of ``ComfyUIAdapter`` (PROV-FIX-01).

Logic is tested with a scripted in-memory transport plus injected sleep/clock;
the wire format is tested against a stdlib HTTP server on 127.0.0.1 through the
default ``UrllibTransport``. No test needs a real ComfyUI or the internet.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from ai_youtube_agent.providers.image_generation import (
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGeneratorErrorCode,
)
from ai_youtube_agent.providers.mock_image_generation import (
    DeterministicSceneImageGenerator,
)
from ai_youtube_agent.providers.registry.comfyui_adapter import (
    ComfyTransportError,
    ComfyUIAdapter,
    HttpResponse,
    UrllibTransport,
    derive_seed,
)
from media_fixtures import make_png

CKPT = "sd_xl_base_1.0.safetensors"
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
CODES = ImageGeneratorErrorCode

# Pinned: sha256-derived seed of make_request() with no explicit seed_key.
GOLDEN_SEED = 3372049896461378094


def make_request(
    *, width: int = 640, height: int = 640, **kwargs: Any
) -> ImageGenerationRequest:
    values: dict[str, Any] = {
        "scene_id": "scene_01",
        "shot_id": "shot_01",
        "prompt": "A quiet harbour at dawn",
        "width": width,
        "height": height,
    }
    values.update(kwargs)
    return ImageGenerationRequest(**values)


def json_response(payload: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode("utf-8"))


def checkpoint_info(names: list[str]) -> dict[str, Any]:
    return {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [names]}}}}


def history_done(
    prompt_id: str = "p1", images: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if images is None:
        images = [{"filename": "out_00001_.png", "subfolder": "", "type": "output"}]
    return {
        prompt_id: {
            "outputs": {"9": {"images": images}},
            "status": {"status_str": "success", "completed": True, "messages": []},
        }
    }


@dataclass
class Call:
    method: str
    path: str
    query: dict[str, list[str]]
    body: bytes | None
    headers: dict[str, str]


@dataclass
class FakeComfy:
    """Scripted ComfyUI: each route pops its next scripted answer (last repeats)."""

    png: bytes
    object_info: list[Any] = field(default_factory=list)
    post: list[Any] = field(default_factory=list)
    history: list[Any] = field(default_factory=list)
    view: list[Any] = field(default_factory=list)
    free: list[Any] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.object_info = self.object_info or [json_response(checkpoint_info([CKPT]))]
        self.post = self.post or [
            json_response({"prompt_id": "p1", "number": 1, "node_errors": {}})
        ]
        self.history = self.history or [
            json_response({}),
            json_response(history_done()),
        ]
        self.view = self.view or [HttpResponse(200, self.png)]
        self.free = self.free or [HttpResponse(200, b"")]

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        parts = urlsplit(url)
        self.calls.append(
            Call(method, parts.path, parse_qs(parts.query), body, dict(headers))
        )
        if parts.path.startswith("/object_info/"):
            return self._next(self.object_info)
        if parts.path == "/prompt":
            return self._next(self.post)
        if parts.path.startswith("/history/"):
            return self._next(self.history)
        if parts.path == "/view":
            return self._next(self.view)
        if parts.path == "/free":
            return self._next(self.free)
        return HttpResponse(404, b"{}")

    @staticmethod
    def _next(script: list[Any]) -> HttpResponse:
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def paths(self) -> list[str]:
        return [call.path for call in self.calls]


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake() -> FakeComfy:
    return FakeComfy(png=make_png(640, 640))


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def make_adapter(fake: FakeComfy, fake_time: FakeTime) -> Callable[..., ComfyUIAdapter]:
    def build(**kwargs: Any) -> ComfyUIAdapter:
        values: dict[str, Any] = {
            "default_model": CKPT,
            "transport": fake,
            "sleep": fake_time.sleep,
            "clock": fake_time.clock,
        }
        values.update(kwargs)
        return ComfyUIAdapter(**values)

    return build


def failure(
    adapter: ComfyUIAdapter, request: ImageGenerationRequest
) -> ImageGenerationError:
    with pytest.raises(ImageGenerationError) as info:
        adapter.generate(request)
    return info.value


# -----------------------------------------------------------------------------
# Success path
# -----------------------------------------------------------------------------


def test_generate_returns_real_downloaded_bytes(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter()
    request = make_request()
    image = adapter.generate(request)

    assert image.data == fake.png
    assert image.sha256 == hashlib.sha256(fake.png).hexdigest()
    assert image.provider == "comfyui"
    assert image.model_name == CKPT
    assert image.media_type == "image/png"
    assert (image.width, image.height) == (640, 640)
    assert image.prompt_hash == request.prompt_hash
    assert fake.paths() == [
        "/object_info/CheckpointLoaderSimple",
        "/prompt",
        "/history/p1",
        "/history/p1",
        "/view",
    ]


def test_generate_records_the_job(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter(client_id="client-abc")
    assert adapter.last_job is None
    request = make_request()
    adapter.generate(request)

    job = adapter.last_job
    assert job is not None
    assert job.prompt_id == "p1"
    assert job.seed == derive_seed(request)
    assert job.checkpoint == CKPT
    assert job.client_id == "client-abc"
    assert job.prompt_hash == request.prompt_hash
    assert (job.output_filename, job.output_subfolder, job.output_type) == (
        "out_00001_.png",
        "",
        "output",
    )
    assert job.polls == 2


def test_post_body_carries_client_id_seed_and_checkpoint(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter()
    request = make_request(seed_key="1234")
    adapter.generate(request)

    post = next(call for call in fake.calls if call.path == "/prompt")
    payload = json.loads(post.body or b"")
    assert payload["client_id"] == adapter.client_id
    graph = payload["prompt"]
    assert graph["3"]["inputs"]["seed"] == 1234
    assert graph["4"]["inputs"]["ckpt_name"] == CKPT
    assert graph["5"]["inputs"]["width"] == 640
    assert post.headers["Content-Type"] == "application/json"


def test_scene_id_is_sanitised_in_the_filename_prefix(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter()
    adapter.generate(make_request(scene_id="scene/../x y"))

    post = next(call for call in fake.calls if call.path == "/prompt")
    graph = json.loads(post.body or b"")["prompt"]
    assert graph["9"]["inputs"]["filename_prefix"] == "s2v_scene____x_y"

    long_graph = adapter._build_prompt_graph(make_request(scene_id="a" * 200), 1)
    assert long_graph["9"]["inputs"]["filename_prefix"] == "s2v_" + "a" * 64


def test_api_key_is_sent_as_bearer_token_on_every_call(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter(api_key="secret-token")
    adapter.generate(make_request())
    assert fake.calls
    assert all(
        call.headers.get("Authorization") == "Bearer secret-token"
        for call in fake.calls
    )

    fake.calls.clear()
    adapter.check_connection()
    assert fake.calls[0].headers.get("Authorization") == "Bearer secret-token"


def test_no_authorization_header_without_api_key(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    make_adapter().generate(make_request())
    assert all("Authorization" not in call.headers for call in fake.calls)


def test_output_selection_prefers_save_image_node_and_encodes_the_query(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.history = [
        json_response(
            {
                "p1": {
                    "outputs": {
                        "10": {
                            "images": [
                                {
                                    "filename": "preview.png",
                                    "subfolder": "",
                                    "type": "temp",
                                }
                            ]
                        },
                        "9": {
                            "images": [
                                {
                                    "filename": "my image.png",
                                    "subfolder": "s2v/day 1",
                                    "type": "output",
                                }
                            ]
                        },
                    },
                    "status": {"status_str": "success", "completed": True},
                }
            }
        )
    ]
    adapter = make_adapter()
    adapter.generate(make_request())

    view = next(call for call in fake.calls if call.path == "/view")
    assert view.query == {
        "filename": ["my image.png"],
        "subfolder": ["s2v/day 1"],
        "type": ["output"],
    }


def test_image_size_is_the_decoded_size_within_tolerance(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter()
    image = adapter.generate(make_request(width=648, height=640))
    assert (image.width, image.height) == (640, 640)


def test_image_size_mismatch_beyond_eight_pixels_fails(
    make_adapter: Callable[..., ComfyUIAdapter],
) -> None:
    error = failure(make_adapter(), make_request(width=649, height=640))
    assert error.error_code is CODES.GENERATION_FAILED
    assert "640x640" in str(error)
    assert "649x640" in str(error)


# -----------------------------------------------------------------------------
# Failures: nothing is ever replaced by a mock image
# -----------------------------------------------------------------------------


def test_server_down_raises_unavailable_without_mock_image(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.object_info = [ComfyTransportError("connection refused")]
    adapter = make_adapter()
    assert adapter.fallback_enabled is False

    error = failure(adapter, make_request())
    assert error.error_code is CODES.UNAVAILABLE
    assert error.provider == "comfyui"
    assert "connection refused" in str(error)
    assert adapter.last_job is None
    assert fake.paths() == ["/object_info/CheckpointLoaderSimple"]


def test_checkpoint_listing_http_error_is_unavailable(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.object_info = [HttpResponse(503, b"")]
    assert failure(make_adapter(), make_request()).error_code is CODES.UNAVAILABLE


def test_missing_checkpoint_is_not_configured(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    installed = [f"model_{index}.safetensors" for index in range(12)]
    fake.object_info = [json_response(checkpoint_info(installed))]
    error = failure(make_adapter(), make_request())

    assert error.error_code is CODES.NOT_CONFIGURED
    assert CKPT in str(error)
    assert "model_0.safetensors" in str(error)
    assert "model_9.safetensors" in str(error)
    assert "model_10.safetensors" not in str(error)
    assert "/prompt" not in fake.paths()


@pytest.mark.parametrize(
    "payload",
    [{"foo": 1}, {"CheckpointLoaderSimple": {"input": {}}}, ["not", "a", "dict"]],
)
def test_unexpected_checkpoint_shape_is_generation_failed(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter], payload: Any
) -> None:
    fake.object_info = [json_response(payload)]
    assert failure(make_adapter(), make_request()).error_code is CODES.GENERATION_FAILED


def test_combo_checkpoint_shape_is_understood(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.object_info = [
        json_response(
            {
                "CheckpointLoaderSimple": {
                    "input": {"required": {"ckpt_name": ["COMBO", {"options": [CKPT]}]}}
                }
            }
        )
    ]
    assert make_adapter().check_models().valid is True


def test_rejected_workflow_is_invalid_request_with_node_errors(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.post = [
        json_response(
            {
                "error": {"type": "prompt_outputs_failed_validation", "message": "bad"},
                "node_errors": {
                    "3": {
                        "class_type": "KSampler",
                        "errors": [{"message": "Value 0 smaller than min 1"}],
                    }
                },
            },
            status=400,
        )
    ]
    error = failure(make_adapter(), make_request())

    assert error.error_code is CODES.INVALID_REQUEST
    assert "KSampler" in str(error)
    assert "smaller than min" in str(error)
    assert len(str(error)) < 700


def test_node_errors_in_a_200_answer_are_invalid_request(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.post = [
        json_response(
            {
                "prompt_id": "p1",
                "node_errors": {"4": {"class_type": "CheckpointLoaderSimple"}},
            }
        )
    ]
    assert failure(make_adapter(), make_request()).error_code is CODES.INVALID_REQUEST


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, CODES.NOT_CONFIGURED),
        (403, CODES.NOT_CONFIGURED),
        (422, CODES.INVALID_REQUEST),
        (429, CODES.UNAVAILABLE),
        (500, CODES.UNAVAILABLE),
        (503, CODES.UNAVAILABLE),
    ],
)
def test_post_status_mapping(
    fake: FakeComfy,
    make_adapter: Callable[..., ComfyUIAdapter],
    status: int,
    code: ImageGeneratorErrorCode,
) -> None:
    fake.post = [HttpResponse(status, b"{}")]
    assert failure(make_adapter(), make_request()).error_code is code


def test_post_transport_error_is_unavailable(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.post = [ComfyTransportError("reset by peer")]
    assert failure(make_adapter(), make_request()).error_code is CODES.UNAVAILABLE


def test_post_without_prompt_id_is_generation_failed(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.post = [json_response({"number": 3})]
    assert failure(make_adapter(), make_request()).error_code is CODES.GENERATION_FAILED


def test_execution_error_in_history_is_generation_failed(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.history = [
        json_response(
            {
                "p1": {
                    "outputs": {},
                    "status": {
                        "status_str": "error",
                        "completed": False,
                        "messages": [
                            [
                                "execution_error",
                                {
                                    "node_type": "KSampler",
                                    "exception_message": "CUDA out of memory",
                                },
                            ]
                        ],
                    },
                }
            }
        )
    ]
    error = failure(make_adapter(), make_request())

    assert error.error_code is CODES.GENERATION_FAILED
    assert "CUDA out of memory" in str(error)
    assert "KSampler" in str(error)
    assert "/view" not in fake.paths()


def test_job_that_never_finishes_times_out_as_unavailable(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter], fake_time: FakeTime
) -> None:
    fake.history = [json_response({})]
    adapter = make_adapter(job_timeout_seconds=3.0, poll_interval_seconds=1.0)
    error = failure(adapter, make_request())

    assert error.error_code is CODES.UNAVAILABLE
    assert "timed out after 3s" in str(error)
    assert "p1" in str(error)
    assert fake_time.now == 3.0
    assert adapter.last_job is None


def test_pending_404_history_is_not_a_failure(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.history = [HttpResponse(404, b""), json_response(history_done())]
    assert make_adapter().generate(make_request()).data == fake.png


def test_finished_job_without_images_fails(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.history = [json_response(history_done(images=[]))]
    error = failure(make_adapter(), make_request())
    assert error.error_code is CODES.GENERATION_FAILED
    assert "without images" in str(error)


@pytest.mark.parametrize("view", [HttpResponse(200, b""), "truncated", "garbage"])
def test_empty_or_corrupt_download_fails(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter], view: Any
) -> None:
    if view == "truncated":
        view = HttpResponse(200, fake.png[:-30])
    elif view == "garbage":
        view = HttpResponse(200, b"<html>not an image</html>")
    fake.view = [view]
    error = failure(make_adapter(), make_request())
    assert error.error_code is CODES.GENERATION_FAILED
    assert "empty or corrupt" in str(error)


def test_missing_output_file_is_generation_failed(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.view = [HttpResponse(404, b"")]
    assert failure(make_adapter(), make_request()).error_code is CODES.GENERATION_FAILED


def test_transient_poll_errors_under_the_limit_are_retried(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.history = [
        ComfyTransportError("blip"),
        HttpResponse(502, b""),
        HttpResponse(200, b"not json"),
        json_response(history_done()),
    ]
    image = make_adapter(max_transient_retries=3).generate(make_request())
    assert image.data == fake.png


def test_transient_poll_errors_over_the_limit_fail_as_unavailable(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.history = [ComfyTransportError("blip")]
    error = failure(make_adapter(max_transient_retries=3), make_request())
    assert error.error_code is CODES.UNAVAILABLE
    assert "4 times in a row" in str(error)


def test_transient_counter_resets_after_a_good_poll(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    blip = ComfyTransportError("blip")
    fake.history = [
        blip,
        blip,
        json_response({}),
        blip,
        blip,
        json_response(history_done()),
    ]
    image = make_adapter(max_transient_retries=2).generate(make_request())
    assert image.data == fake.png


def test_transient_download_errors_are_bounded(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.view = [HttpResponse(503, b""), HttpResponse(200, fake.png)]
    assert make_adapter().generate(make_request()).data == fake.png

    fake.view = [HttpResponse(503, b"")]
    error = failure(make_adapter(max_transient_retries=2), make_request())
    assert error.error_code is CODES.UNAVAILABLE


# -----------------------------------------------------------------------------
# Explicit fallback (opt-in only)
# -----------------------------------------------------------------------------


def test_there_is_no_fallback_by_default() -> None:
    adapter = ComfyUIAdapter()
    assert adapter.fallback_enabled is False
    assert ComfyUIAdapter(
        fallback_generator=DeterministicSceneImageGenerator()
    ).fallback_enabled


def test_explicit_fallback_is_used_on_unavailable_with_its_own_provider_name(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.object_info = [ComfyTransportError("connection refused")]
    fallback = DeterministicSceneImageGenerator()
    adapter = make_adapter(fallback_generator=fallback)
    image = adapter.generate(make_request())

    assert image.provider == "local_deterministic_png"
    assert image.provider != "comfyui"
    assert len(fallback.calls) == 1
    assert adapter.last_job is None


@pytest.mark.parametrize("scenario", ["not_configured", "generation_failed"])
def test_explicit_fallback_covers_other_provider_errors(
    fake: FakeComfy,
    make_adapter: Callable[..., ComfyUIAdapter],
    scenario: str,
) -> None:
    if scenario == "not_configured":
        fake.object_info = [json_response(checkpoint_info(["other.safetensors"]))]
    else:
        fake.history = [json_response(history_done(images=[]))]
    fallback = DeterministicSceneImageGenerator()
    image = make_adapter(fallback_generator=fallback).generate(make_request())
    assert image.provider == "local_deterministic_png"


def test_fallback_is_not_used_for_invalid_request(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.post = [json_response({"error": {"message": "bad"}}, status=400)]
    fallback = DeterministicSceneImageGenerator()
    error = failure(make_adapter(fallback_generator=fallback), make_request())

    assert error.error_code is CODES.INVALID_REQUEST
    assert fallback.calls == []


# -----------------------------------------------------------------------------
# Seed
# -----------------------------------------------------------------------------


def test_explicit_seed_key_is_the_seed() -> None:
    assert derive_seed(make_request(seed_key="1234")) == 1234
    assert derive_seed(make_request(seed_key="0")) == 0
    assert derive_seed(make_request(seed_key=str(2**63 - 1))) == 2**63 - 1


@pytest.mark.parametrize(
    "seed_key",
    ["", "007", "-5", "12.5", " 7", "7 ", "abc", str(2**63), "9" * 25, "１２３"],
)
def test_other_seed_keys_derive_a_stable_seed(seed_key: str) -> None:
    request = make_request(seed_key=seed_key)
    seed = derive_seed(request)
    assert 0 <= seed <= 2**63 - 1
    assert seed != (int(seed_key) if seed_key.isascii() and seed_key.isdigit() else -1)
    assert seed == derive_seed(request)


def test_derived_seed_varies_with_the_prompt() -> None:
    first = derive_seed(make_request(prompt="a quiet harbour"))
    second = derive_seed(make_request(prompt="a busy market"))
    assert first != second


def test_derived_seed_matches_the_pinned_golden_value() -> None:
    assert derive_seed(make_request()) == GOLDEN_SEED


@pytest.mark.parametrize("hash_seed", ["1", "2"])
def test_derived_seed_is_stable_across_processes(hash_seed: str) -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from ai_youtube_agent.providers.image_generation import "
        "ImageGenerationRequest;"
        "from ai_youtube_agent.providers.registry.comfyui_adapter import "
        "derive_seed;"
        "print(derive_seed(ImageGenerationRequest(scene_id='scene_01', "
        "shot_id='shot_01', prompt='A quiet harbour at dawn', width=640, "
        "height=640)))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(SRC_DIR)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        timeout=60,
    )
    assert int(result.stdout.strip()) == derive_seed(make_request()) == GOLDEN_SEED


# -----------------------------------------------------------------------------
# Checks and construction
# -----------------------------------------------------------------------------


def test_check_models_reports_installed_and_missing_checkpoints(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    ok = make_adapter().check_models()
    assert ok.valid is True
    assert ok.available_models == (CKPT,)
    assert ok.missing_models == ()

    fake.object_info = [json_response(checkpoint_info(["other.safetensors"]))]
    missing = make_adapter().check_models()
    assert missing.valid is False
    assert missing.missing_models == (CKPT,)
    assert CKPT in (missing.error_message or "")


def test_check_models_reports_unparsable_answers(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.object_info = [json_response({"foo": 1})]
    result = make_adapter().check_models()
    assert result.valid is False
    assert result.error_message


def test_check_connection_reports_http_errors_as_offline(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    result = make_adapter().check_connection()  # the fake answers 404 here
    assert result.connected is False
    assert "unreachable" in (result.error_message or "").lower()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"poll_interval_seconds": 0},
        {"job_timeout_seconds": -1},
        {"max_transient_retries": -1},
    ],
)
def test_invalid_constructor_values_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ComfyUIAdapter(**kwargs)


def test_client_id_defaults_to_a_random_hex_id() -> None:
    first, second = ComfyUIAdapter(), ComfyUIAdapter()
    assert len(first.client_id) == 32
    assert first.client_id != second.client_id


# -----------------------------------------------------------------------------
# Wire tests: the default UrllibTransport against a local HTTP server
# -----------------------------------------------------------------------------


class _ServerState:
    def __init__(self, png: bytes) -> None:
        self.png = png
        self.reject_prompt = False
        self.requests: list[tuple[str, str]] = []
        self.history_polls = 0
        self.authorization: list[str | None] = []


@dataclass
class RunningServer:
    url: str
    state: _ServerState


def _handler_for(state: _ServerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            state.requests.append(("GET", self.path))
            state.authorization.append(self.headers.get("Authorization"))
            path = urlsplit(self.path).path
            if path.startswith("/object_info/"):
                self._send_json(200, checkpoint_info([CKPT]))
            elif path == "/system_stats":
                self._send_json(200, {"devices": []})
            elif path.startswith("/history/"):
                state.history_polls += 1
                if state.history_polls < 2:
                    self._send_json(200, {})
                else:
                    self._send_json(200, history_done("wire-1"))
            elif path == "/view":
                self._send(200, state.png, "image/png")
            else:
                self._send_json(404, {})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            state.requests.append(("POST", self.path))
            if state.reject_prompt:
                self._send_json(
                    400,
                    {
                        "error": {"message": "Prompt outputs failed validation"},
                        "node_errors": {
                            "3": {
                                "class_type": "KSampler",
                                "errors": [{"message": "bad sampler"}],
                            }
                        },
                    },
                )
            else:
                self._send_json(200, {"prompt_id": "wire-1", "node_errors": {}})

    return Handler


@pytest.fixture
def comfy_server(request: pytest.FixtureRequest) -> Iterator[RunningServer]:
    state = _ServerState(make_png(640, 640))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    request.addfinalizer(stop)
    yield RunningServer(url=f"http://127.0.0.1:{server.server_port}", state=state)


def wire_adapter(url: str, **kwargs: Any) -> ComfyUIAdapter:
    return ComfyUIAdapter(
        base_url=url,
        default_model=CKPT,
        timeout_seconds=5.0,
        poll_interval_seconds=0.01,
        **kwargs,
    )


def test_wire_end_to_end_success(comfy_server: RunningServer) -> None:
    adapter = wire_adapter(comfy_server.url, api_key="wire-key")
    image = adapter.generate(make_request())

    assert image.data == comfy_server.state.png
    assert image.provider == "comfyui"
    assert adapter.last_job is not None
    assert adapter.last_job.prompt_id == "wire-1"
    assert ("POST", "/prompt") in comfy_server.state.requests
    assert any(path.startswith("/view?") for _, path in comfy_server.state.requests)
    assert set(comfy_server.state.authorization) == {"Bearer wire-key"}
    assert adapter.check_connection().connected is True


def test_wire_rejected_workflow_is_invalid_request(
    comfy_server: RunningServer,
) -> None:
    comfy_server.state.reject_prompt = True
    error = failure(wire_adapter(comfy_server.url), make_request())

    assert error.error_code is CODES.INVALID_REQUEST
    assert "KSampler" in str(error)
    assert "bad sampler" in str(error)


def test_wire_closed_port_is_unavailable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    adapter = ComfyUIAdapter(
        base_url=f"http://127.0.0.1:{port}", timeout_seconds=1.0, default_model=CKPT
    )
    error = failure(adapter, make_request())

    assert error.error_code is CODES.UNAVAILABLE
    assert adapter.last_job is None


def test_urllib_transport_returns_http_error_statuses(
    comfy_server: RunningServer,
) -> None:
    response = UrllibTransport().request(
        "GET", f"{comfy_server.url}/nope", body=None, headers={}, timeout=5.0
    )
    assert response.status == 404
    assert response.body == b"{}"


# -----------------------------------------------------------------------------
# Asset storage persistence & metadata verification
# -----------------------------------------------------------------------------


def test_generate_and_store_persists_to_artifact_storage_and_verifies(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter], tmp_path: Path
) -> None:
    from ai_youtube_agent.core.artifact import ArtifactKind
    from ai_youtube_agent.core.artifact_storage import LocalFileStorage

    storage = LocalFileStorage(tmp_path / "artifacts")
    adapter = make_adapter()
    req = make_request(seed_key="7777")
    image, record, stored = adapter.generate_and_store(
        req,
        storage,
        content_item_id="item_comfy_01",
        version=1,
        kind=ArtifactKind.THUMBNAIL,
    )

    assert stored.sha256 == image.sha256
    assert stored.size_bytes == len(fake.png)
    assert storage.read(stored.uri) == fake.png
    assert record.stored_uri == stored.uri
    assert record.seed == 7777
    metadata = record.as_metadata(image)
    assert metadata["prompt_id"] == "p1"
    assert metadata["seed"] == 7777
    assert metadata["checkpoint"] == CKPT
    assert metadata["sha256"] == image.sha256
    assert metadata["stored_uri"] == stored.uri
    assert req.prompt not in json.dumps(metadata)


def test_save_to_directory_and_asset_output_dir_write_image_and_metadata(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter], tmp_path: Path
) -> None:
    out_dir = tmp_path / "comfy_assets"
    adapter = make_adapter(asset_output_dir=out_dir)
    req = make_request(seed_key="42")
    image = adapter.generate(req)

    assert adapter.last_job is not None
    assert adapter.last_job.stored_uri is not None
    assert adapter.last_job.metadata_uri is not None

    img2, rec2, img_path, meta_path = adapter.save_to_directory(
        req, tmp_path / "direct_save"
    )
    assert img_path.exists()
    assert img_path.read_bytes() == fake.png
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["prompt_id"] == rec2.prompt_id
    assert meta["seed"] == 42
    assert meta["sha256"] == img2.sha256 == image.sha256
    assert meta["width"] == 640
    assert meta["height"] == 640


def test_capabilities_image_gen_and_check_require_installed_checkpoint(
    comfy_server: RunningServer,
) -> None:
    from ai_youtube_agent.core.errors import ProviderError

    ok_adapter = wire_adapter(comfy_server.url)
    assert ok_adapter.capabilities_image_gen().available is True
    ok_adapter.check()

    missing_adapter = ComfyUIAdapter(
        base_url=comfy_server.url,
        default_model="missing.safetensors",
        timeout_seconds=5.0,
    )
    caps = missing_adapter.capabilities_image_gen()
    assert caps.available is False
    assert "missing.safetensors" in (caps.reason or "")
    with pytest.raises(ProviderError):
        missing_adapter.check()


# -----------------------------------------------------------------------------
# Custom workflow graph hook (ANIM-01) and free_memory
# -----------------------------------------------------------------------------


def custom_graph(seed: int = 0) -> dict[str, Any]:
    return {
        "1": {"class_type": "Marker", "inputs": {"seed": seed}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }


def posted_graph(fake: FakeComfy) -> dict[str, Any]:
    post = next(call for call in fake.calls if call.path == "/prompt")
    return json.loads(post.body or b"")["prompt"]


def test_graph_builder_replaces_the_default_workflow(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter()
    request = make_request(seed_key="77")
    seen: list[tuple[ImageGenerationRequest, int]] = []

    def builder(req: ImageGenerationRequest, seed: int) -> dict[str, Any]:
        seen.append((req, seed))
        return custom_graph(seed)

    image, record = adapter.generate_with_job(request, graph_builder=builder)

    assert seen == [(request, 77)]
    assert posted_graph(fake) == custom_graph(77)
    assert record.seed == 77 and image.data == fake.png


def test_without_a_builder_the_default_graph_is_still_posted(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter()
    request = make_request(seed_key="5")

    adapter.generate_with_job(request)

    assert posted_graph(fake) == adapter._build_prompt_graph(request, 5)


def test_save_to_directory_passes_the_graph_builder(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter], tmp_path: Path
) -> None:
    adapter = make_adapter()

    _, record, img_path, meta_path = adapter.save_to_directory(
        make_request(seed_key="9"),
        tmp_path / "custom",
        graph_builder=lambda req, seed: custom_graph(seed),
    )

    assert posted_graph(fake) == custom_graph(9)
    assert img_path.read_bytes() == fake.png
    assert json.loads(meta_path.read_text(encoding="utf-8"))["seed"] == 9
    assert adapter.last_job == record


def test_an_empty_custom_graph_is_rejected_before_any_submission(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter()

    with pytest.raises(ImageGenerationError) as info:
        adapter.generate_with_job(make_request(), graph_builder=lambda req, seed: {})

    assert info.value.error_code is CODES.INVALID_REQUEST
    assert "/prompt" not in fake.paths()


def test_a_custom_graph_failure_keeps_the_normal_error_mapping(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    fake.history = [
        json_response(
            {
                "p1": {
                    "status": {
                        "status_str": "error",
                        "messages": [
                            [
                                "execution_error",
                                {
                                    "node_type": "VAEEncode",
                                    "exception_message": "CUDA out of memory",
                                },
                            ]
                        ],
                    },
                    "outputs": {},
                }
            }
        )
    ]
    adapter = make_adapter()

    with pytest.raises(ImageGenerationError) as info:
        adapter.generate_with_job(
            make_request(), graph_builder=lambda req, seed: custom_graph(seed)
        )

    assert info.value.error_code is CODES.GENERATION_FAILED
    assert "CUDA out of memory" in str(info.value)


def test_free_memory_posts_to_free_and_never_raises(
    fake: FakeComfy, make_adapter: Callable[..., ComfyUIAdapter]
) -> None:
    adapter = make_adapter()

    assert adapter.free_memory() is True

    call = fake.calls[-1]
    assert (call.method, call.path) == ("POST", "/free")
    assert json.loads(call.body or b"") == {"unload_models": True, "free_memory": True}
    fake.free = [HttpResponse(500, b"")]
    assert adapter.free_memory() is False
    fake.free = [ComfyTransportError("down")]
    assert adapter.free_memory() is False
