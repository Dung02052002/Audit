"""ComfyUI Provider Adapter.

Integrates a local ComfyUI instance for AI image generation, custom workflows,
and optional local video generation models (AnimateDiff, SVD, Wan, CogVideo).

``generate`` runs the real ComfyUI job: it checks the checkpoint, queues the
workflow (``POST /prompt``), polls ``GET /history/{prompt_id}`` until the job
finishes, downloads the image (``GET /view``) and verifies that the bytes are a
complete PNG, JPEG or WebP of the requested size. It never returns an image
that ComfyUI did not produce. When ComfyUI is down or the job fails it raises
``ImageGenerationError``; a ``fallback_generator`` is used only when the caller
passes one explicitly, and only for provider errors (never for a bad request).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.artifact_storage import (
    MEDIA_EXTENSIONS,
    ArtifactStorage,
    StoredFile,
)
from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.image_generation import (
    GeneratedImage,
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGenerator,
    ImageGeneratorCapabilities,
    ImageGeneratorErrorCode,
)
from ai_youtube_agent.providers.media_probe import MediaProbeError, probe_image_bytes
from ai_youtube_agent.providers.registry.base import BaseProviderAdapter
from ai_youtube_agent.providers.registry.models import (
    ConnectionCheckResult,
    CredentialCheckResult,
    ModelCheckResult,
    ProviderCapability,
    ProviderCategory,
    ResourceCheckResult,
    ResourceRequirement,
)

logger = get_logger(__name__)

DEFAULT_COMFYUI_CHECKPOINT: Final[str] = "sd_xl_base_1.0.safetensors"
MAX_SEED: Final[int] = 2**63 - 1
SIZE_TOLERANCE_PX: Final[int] = 8
_MAX_RESPONSE_BYTES: Final[int] = 64 * 1024 * 1024
_MAX_DETAIL_CHARS: Final[int] = 500
_MAX_LISTED_CHECKPOINTS: Final[int] = 10
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
_SEED_PATTERN: Final[re.Pattern[str]] = re.compile(r"(0|[1-9][0-9]{0,18})")
_PREFIX_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_-]")
_FALLBACK_CODES: Final[frozenset[ImageGeneratorErrorCode]] = frozenset(
    {
        ImageGeneratorErrorCode.UNAVAILABLE,
        ImageGeneratorErrorCode.NOT_CONFIGURED,
        ImageGeneratorErrorCode.GENERATION_FAILED,
    }
)


def derive_seed(request: ImageGenerationRequest) -> int:
    """Return the sampler seed for ``request``.

    An explicit decimal ``seed_key`` (no leading zeros, at most 2**63-1) is the
    seed. Otherwise the seed is derived from the request's ``prompt_hash`` with
    SHA-256, so it is stable across processes (never Python's salted ``hash``).
    """
    key = request.seed_key
    if _SEED_PATTERN.fullmatch(key) and int(key) <= MAX_SEED:
        return int(key)
    digest = hashlib.sha256(b"comfyui-seed|" + request.prompt_hash.encode()).digest()
    return int.from_bytes(digest[:8], "big") & MAX_SEED


# -----------------------------------------------------------------------------
# HTTP transport (injectable so tests can script ComfyUI without a network)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class ComfyTransportError(Exception):
    """The HTTP request could not be completed (no response was received)."""


class ComfyTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Default transport on ``urllib``; HTTP error statuses are returned."""

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        host = urllib.parse.urlsplit(url).hostname or ""
        if host in _LOOPBACK_HOSTS:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            opener = urllib.request.build_opener()
        try:
            with opener.open(request, timeout=timeout) as response:
                data = response.read(_MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                return HttpResponse(exc.code, exc.read(_MAX_RESPONSE_BYTES + 1))
            finally:
                exc.close()
        except (
            urllib.error.URLError,
            OSError,
            TimeoutError,
            http.client.HTTPException,
        ) as exc:
            raise ComfyTransportError(str(exc)) from exc
        if len(data) > _MAX_RESPONSE_BYTES:
            raise ComfyTransportError("response is larger than 64 MiB")
        return HttpResponse(status, data)


@dataclass(frozen=True)
class ComfyJobRecord:
    """Provenance of one finished ComfyUI job (never contains prompt text)."""

    prompt_id: str
    seed: int
    checkpoint: str
    client_id: str
    prompt_hash: str
    output_filename: str
    output_subfolder: str
    output_type: str
    polls: int
    elapsed_seconds: float
    stored_uri: str | None = None
    metadata_uri: str | None = None

    def as_metadata(
        self, image: GeneratedImage, *, stored_uri: str | None = None
    ) -> dict[str, Any]:
        """Structured metadata dictionary suitable for persistence (no prompt text)."""
        return {
            "provider": image.provider,
            "prompt_id": self.prompt_id,
            "seed": self.seed,
            "checkpoint": self.checkpoint,
            "client_id": self.client_id,
            "prompt_hash": self.prompt_hash,
            "output_filename": self.output_filename,
            "output_subfolder": self.output_subfolder,
            "output_type": self.output_type,
            "polls": self.polls,
            "elapsed_seconds": self.elapsed_seconds,
            "media_type": image.media_type,
            "width": image.width,
            "height": image.height,
            "sha256": image.sha256,
            "size_bytes": len(image.data),
            "generated_at": image.generated_at.isoformat(),
            "stored_uri": stored_uri if stored_uri is not None else self.stored_uri,
        }


def _summary(text: str) -> str:
    text = " ".join(text.split())
    return (
        text
        if len(text) <= _MAX_DETAIL_CHARS
        else text[: _MAX_DETAIL_CHARS - 3] + "..."
    )


def _json_or_none(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


class ComfyUIAdapter(BaseProviderAdapter, ImageGenerator):
    """Unified adapter for local ComfyUI image and workflow generation."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8188",
        api_key: str | None = None,
        default_model: str = DEFAULT_COMFYUI_CHECKPOINT,
        timeout_seconds: float = 5.0,
        enable_video_workflow: bool = False,
        fallback_generator: ImageGenerator | None = None,
        poll_interval_seconds: float = 1.0,
        job_timeout_seconds: float = 300.0,
        max_transient_retries: int = 3,
        client_id: str | None = None,
        asset_output_dir: Path | str | None = None,
        transport: ComfyTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if job_timeout_seconds <= 0:
            raise ValueError("job_timeout_seconds must be positive")
        if max_transient_retries < 0:
            raise ValueError("max_transient_retries must not be negative")
        super().__init__(
            provider_id="comfyui",
            category=ProviderCategory.IMAGE_GENERATION,
        )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds
        self.enable_video_workflow = enable_video_workflow
        self.poll_interval_seconds = poll_interval_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self.max_transient_retries = max_transient_retries
        self.client_id = client_id or uuid.uuid4().hex
        self.asset_output_dir = (
            Path(asset_output_dir).resolve() if asset_output_dir is not None else None
        )
        self._fallback = fallback_generator
        self._transport: ComfyTransport = transport or UrllibTransport()
        self._sleep = sleep
        self._clock = clock
        self._job_lock = threading.Lock()
        self._last_job: ComfyJobRecord | None = None
        self.name = "comfyui"

    @property
    def fallback_enabled(self) -> bool:
        return self._fallback is not None

    @property
    def last_job(self) -> ComfyJobRecord | None:
        with self._job_lock:
            return self._last_job

    def capabilities(self) -> ProviderCapability:
        modalities = ["image", "workflow_api", "text2image", "img2img"]
        if self.enable_video_workflow:
            modalities.append("video_clip")

        return ProviderCapability(
            provider_id=self.provider_id,
            name="ComfyUI Local AI Engine",
            category=self.category,
            is_local=True,
            is_free=True,
            supported_modalities=tuple(modalities),
            supported_models=(
                self.default_model,
                "flux1-schnell.safetensors",
                "v1-5-pruned-emaonly.safetensors",
            ),
            resource_requirements=ResourceRequirement(
                ram_mb_min=8192,
                vram_mb_min=6144,
                disk_space_mb_min=10240,
                requires_gpu=True,
                executable_name="python",
            ),
            metadata={
                "base_url": self.base_url,
                "video_workflow_enabled": self.enable_video_workflow,
            },
        )

    # -------------------------------------------------------------------------
    # HTTP helpers
    # -------------------------------------------------------------------------

    def _request(
        self, method: str, path: str, *, body: bytes | None = None
    ) -> HttpResponse:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return self._transport.request(
            method,
            f"{self.base_url}{path}",
            body=body,
            headers=headers,
            timeout=self.timeout_seconds,
        )

    def _get_json(self, path: str) -> Any:
        response = self._request("GET", path)
        if response.status != 200:
            raise ComfyTransportError(f"HTTP {response.status} from {path}")
        return json.loads(response.body.decode("utf-8"))

    def _error(
        self, code: ImageGeneratorErrorCode, detail: str
    ) -> ImageGenerationError:
        return ImageGenerationError(code, detail, provider=self.name)

    def _status_error(self, status: int, what: str) -> ImageGenerationError:
        """Map an unexpected HTTP status of a non-POST request to an error."""
        if status in (401, 403):
            return self._error(
                ImageGeneratorErrorCode.NOT_CONFIGURED,
                f"ComfyUI rejected the credentials (HTTP {status}) for {what}.",
            )
        if status == 429 or status >= 500:
            return self._error(
                ImageGeneratorErrorCode.UNAVAILABLE,
                f"ComfyUI answered HTTP {status} for {what}.",
            )
        return self._error(
            ImageGeneratorErrorCode.GENERATION_FAILED,
            f"ComfyUI answered unexpected HTTP {status} for {what}.",
        )

    # -------------------------------------------------------------------------
    # Provider checks
    # -------------------------------------------------------------------------

    def check_connection(self) -> ConnectionCheckResult:
        start_time = time.monotonic()
        try:
            self._get_json("/system_stats")
            latency_ms = (time.monotonic() - start_time) * 1000.0
            return ConnectionCheckResult(
                connected=True,
                latency_ms=round(latency_ms, 2),
                endpoint=self.base_url,
            )
        except Exception as exc:
            return ConnectionCheckResult(
                connected=False,
                endpoint=self.base_url,
                error_message=f"ComfyUI unreachable at {self.base_url}: {exc}",
            )

    def check_models(self) -> ModelCheckResult:
        try:
            names = self._fetch_checkpoints()
        except ImageGenerationError as exc:
            return ModelCheckResult(
                valid=False,
                available_models=(),
                default_model=self.default_model,
                missing_models=(self.default_model,),
                error_message=f"Could not retrieve ComfyUI model list: {exc}",
            )
        valid = self.default_model in names
        return ModelCheckResult(
            valid=valid,
            available_models=tuple(names[:_MAX_LISTED_CHECKPOINTS]),
            default_model=self.default_model,
            missing_models=() if valid else (self.default_model,),
            error_message=None
            if valid
            else f"Checkpoint '{self.default_model}' is not installed in ComfyUI.",
        )

    def check_credentials(self) -> CredentialCheckResult:
        # ComfyUI is primarily local without auth. If API key is configured, verify it.
        return CredentialCheckResult(
            valid=True,
            has_credentials=True,
            auth_type="bearer_token" if self.api_key else "none",
        )

    def check_resources(self) -> ResourceCheckResult:
        try:
            stats = self._get_json("/system_stats")
            devices = stats.get("devices", [])
            vram_free_mb = 0
            gpu_name = "CPU only"
            if devices:
                dev = devices[0]
                gpu_name = dev.get("name", "GPU")
                vram_free_mb = int(dev.get("vram_free", 0) / (1024 * 1024))

            sufficient = vram_free_mb >= 4000 or len(devices) == 0
            warning = (
                None
                if sufficient
                else f"Low GPU VRAM ({vram_free_mb} MB). Recommend >= 6144 MB."
            )

            return ResourceCheckResult(
                sufficient=sufficient,
                vram_available_mb=vram_free_mb,
                executable_found=True,
                details={"gpu_name": gpu_name, "device_count": len(devices)},
                warning_message=warning,
            )
        except Exception:
            return ResourceCheckResult(
                sufficient=True,
                executable_found=True,
                warning_message="Could not inspect GPU; default resources assumed.",
            )

    # -------------------------------------------------------------------------
    # ImageGenerator Protocol implementation
    # -------------------------------------------------------------------------

    def capabilities_image_gen(self) -> ImageGeneratorCapabilities:
        conn = self.check_connection()
        models = self.check_models() if conn.connected else None
        available = conn.connected and bool(models and models.valid)
        reason: str | None = None
        if not conn.connected:
            reason = conn.error_message
        elif models is not None and not models.valid:
            reason = models.error_message
        return ImageGeneratorCapabilities(
            provider_name=self.name,
            model_name=self.default_model,
            available=available,
            is_mock_or_local=True,
            supports_vertical_9_16=True,
            supports_horizontal_16_9=True,
            reason=reason,
        )

    def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        """Generate one scene image with ComfyUI.

        Raises ``ImageGenerationError`` when ComfyUI cannot deliver. The optional
        explicit fallback generator is used only for provider errors
        (UNAVAILABLE, NOT_CONFIGURED, GENERATION_FAILED), never for a rejected
        request, and its result keeps the fallback's own provider name.
        """
        try:
            image, record = self.generate_with_job(request)
        except ImageGenerationError as exc:
            if self._fallback is None or exc.error_code not in _FALLBACK_CODES:
                raise
            logger.warning(
                "ComfyUI failed, using the explicit fallback generator",
                extra={
                    "fields": {
                        "scene_id": request.scene_id,
                        "error_code": exc.error_code.value,
                        "fallback": getattr(self._fallback, "name", "unknown"),
                    }
                },
            )
            return self._fallback.generate(request)
        with self._job_lock:
            self._last_job = record
        return image

    def generate_with_job(
        self, request: ImageGenerationRequest
    ) -> tuple[GeneratedImage, ComfyJobRecord]:
        """Run the full ComfyUI job and return the image with its job record."""
        checkpoints = self._fetch_checkpoints()
        if self.default_model not in checkpoints:
            listed = ", ".join(checkpoints[:_MAX_LISTED_CHECKPOINTS]) or "none"
            raise self._error(
                ImageGeneratorErrorCode.NOT_CONFIGURED,
                f"Checkpoint '{self.default_model}' is not installed in ComfyUI; "
                f"available: {listed}.",
            )

        seed = derive_seed(request)
        start = self._clock()
        prompt_id = self._queue_prompt(request, seed)
        logger.info(
            "ComfyUI job queued",
            extra={"fields": {"prompt_id": prompt_id, "seed": seed}},
        )
        entry, polls = self._wait_for_job(prompt_id)
        filename, subfolder, image_type = self._pick_output_image(entry)
        data = self._download_image(filename, subfolder, image_type)

        try:
            probe = probe_image_bytes(data)
        except MediaProbeError as exc:
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                f"downloaded image is empty or corrupt: {exc}",
            ) from exc
        if (
            abs(probe.width - request.width) > SIZE_TOLERANCE_PX
            or abs(probe.height - request.height) > SIZE_TOLERANCE_PX
        ):
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                f"ComfyUI returned a {probe.width}x{probe.height} image, "
                f"requested {request.width}x{request.height}.",
            )

        image = GeneratedImage(
            data=data,
            media_type=probe.media_type,
            width=probe.width,
            height=probe.height,
            sha256=hashlib.sha256(data).hexdigest(),
            provider=self.name,
            model_name=self.default_model,
            prompt_hash=request.prompt_hash,
            generated_at=datetime.now(UTC),
        )
        record = ComfyJobRecord(
            prompt_id=prompt_id,
            seed=seed,
            checkpoint=self.default_model,
            client_id=self.client_id,
            prompt_hash=request.prompt_hash,
            output_filename=filename,
            output_subfolder=subfolder,
            output_type=image_type,
            polls=polls,
            elapsed_seconds=round(self._clock() - start, 3),
        )
        if self.asset_output_dir is not None:
            img_path, meta_path = self._persist_to_directory(
                request, image, record, self.asset_output_dir
            )
            record = replace(
                record,
                stored_uri=img_path.as_uri(),
                metadata_uri=meta_path.as_uri(),
            )
        logger.info(
            "ComfyUI job finished",
            extra={
                "fields": {
                    "prompt_id": prompt_id,
                    "seed": seed,
                    "polls": polls,
                    "width": probe.width,
                    "height": probe.height,
                }
            },
        )
        return image, record

    def generate_and_store(
        self,
        request: ImageGenerationRequest,
        storage: ArtifactStorage,
        *,
        content_item_id: str,
        version: int = 1,
        kind: ArtifactKind = ArtifactKind.THUMBNAIL,
    ) -> tuple[GeneratedImage, ComfyJobRecord, StoredFile]:
        """Generate a real ComfyUI image, write to ``storage``, and verify readback."""
        image, record = self.generate_with_job(request)
        try:
            stored = storage.write(
                content_item_id,
                kind,
                version,
                image.data,
                media_type=image.media_type,
            )
            read_back = storage.read(stored.uri)
        except Exception as exc:
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                f"failed to store or read back generated image in asset storage: {exc}",
            ) from exc

        if not read_back or hashlib.sha256(read_back).hexdigest() != image.sha256:
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                "stored image in asset storage is empty or does not match sha256",
            )
        try:
            probe_image_bytes(read_back)
        except MediaProbeError as exc:
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                f"stored image in asset storage is unreadable: {exc}",
            ) from exc

        record = replace(record, stored_uri=stored.uri)
        with self._job_lock:
            self._last_job = record
        return image, record, stored

    def save_to_directory(
        self, request: ImageGenerationRequest, directory: Path | str
    ) -> tuple[GeneratedImage, ComfyJobRecord, Path, Path]:
        """Generate a real ComfyUI image, save image + metadata JSON, and verify."""
        image, record = self.generate_with_job(request)
        img_path, meta_path = self._persist_to_directory(
            request, image, record, Path(directory).resolve()
        )
        record = replace(
            record,
            stored_uri=img_path.as_uri(),
            metadata_uri=meta_path.as_uri(),
        )
        with self._job_lock:
            self._last_job = record
        return image, record, img_path, meta_path

    def _persist_to_directory(
        self,
        request: ImageGenerationRequest,
        image: GeneratedImage,
        record: ComfyJobRecord,
        target_dir: Path,
    ) -> tuple[Path, Path]:
        ext = MEDIA_EXTENSIONS.get(image.media_type, "png")
        safe_scene = _PREFIX_UNSAFE.sub("_", request.scene_id)[:48] or "scene"
        safe_shot = _PREFIX_UNSAFE.sub("_", request.shot_id)[:48] or "shot"
        safe_prompt = _PREFIX_UNSAFE.sub("_", record.prompt_id)[:48] or "job"
        stem = f"{safe_scene}_{safe_shot}_{safe_prompt}"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            img_path = (target_dir / f"{stem}.{ext}").resolve()
            meta_path = (target_dir / f"{stem}.meta.json").resolve()
            img_path.write_bytes(image.data)
            read_back = img_path.read_bytes()
            if not read_back or hashlib.sha256(read_back).hexdigest() != image.sha256:
                raise ValueError("stored image checksum mismatch or empty file")
            probe_image_bytes(read_back)
            metadata = record.as_metadata(image, stored_uri=img_path.as_uri())
            meta_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
            )
        except Exception as exc:
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                f"failed to persist or verify ComfyUI image in {target_dir}: {exc}",
            ) from exc
        return img_path, meta_path

    # -------------------------------------------------------------------------
    # Job steps
    # -------------------------------------------------------------------------

    def _fetch_checkpoints(self) -> list[str]:
        """GET the installed checkpoint names (step 1)."""
        try:
            response = self._request("GET", "/object_info/CheckpointLoaderSimple")
        except ComfyTransportError as exc:
            raise self._error(
                ImageGeneratorErrorCode.UNAVAILABLE,
                f"ComfyUI server at {self.base_url} is unreachable: {exc}",
            ) from exc
        if response.status in (401, 403):
            raise self._status_error(response.status, "the checkpoint list")
        if response.status != 200:
            raise self._error(
                ImageGeneratorErrorCode.UNAVAILABLE,
                f"ComfyUI at {self.base_url} answered HTTP {response.status} "
                "to the checkpoint list request.",
            )
        info = _json_or_none(response.body)
        try:
            entry = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
            if isinstance(entry, list):
                names = entry
            elif entry == "COMBO":
                names = info["CheckpointLoaderSimple"]["input"]["required"][
                    "ckpt_name"
                ][1]["options"]
            else:
                raise TypeError("unknown ckpt_name shape")
            if not isinstance(names, list) or not all(
                isinstance(name, str) for name in names
            ):
                raise TypeError("ckpt_name options are not strings")
        except (KeyError, IndexError, TypeError) as exc:
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                "ComfyUI returned an unexpected checkpoint list.",
            ) from exc
        return names

    def _queue_prompt(self, request: ImageGenerationRequest, seed: int) -> str:
        """POST the workflow and return the queued ``prompt_id`` (step 2)."""
        payload = json.dumps(
            {
                "prompt": self._build_prompt_graph(request, seed),
                "client_id": self.client_id,
            }
        ).encode("utf-8")
        try:
            response = self._request("POST", "/prompt", body=payload)
        except ComfyTransportError as exc:
            raise self._error(
                ImageGeneratorErrorCode.UNAVAILABLE,
                f"ComfyUI server at {self.base_url} is unreachable: {exc}",
            ) from exc

        status = response.status
        data = _json_or_none(response.body)
        if status in (401, 403):
            raise self._error(
                ImageGeneratorErrorCode.NOT_CONFIGURED,
                f"ComfyUI rejected the credentials (HTTP {status}).",
            )
        if status == 429 or status >= 500:
            raise self._error(
                ImageGeneratorErrorCode.UNAVAILABLE,
                f"ComfyUI answered HTTP {status} to the workflow submission.",
            )
        if 400 <= status < 500:
            raise self._error(
                ImageGeneratorErrorCode.INVALID_REQUEST,
                f"ComfyUI rejected the workflow (HTTP {status}): "
                f"{self._describe_rejection(data)}",
            )
        if status != 200 or not isinstance(data, dict):
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                f"ComfyUI answered an unexpected HTTP {status} to the submission.",
            )
        if data.get("node_errors"):
            raise self._error(
                ImageGeneratorErrorCode.INVALID_REQUEST,
                f"ComfyUI reported workflow errors: {self._describe_rejection(data)}",
            )
        prompt_id = data.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                "ComfyUI did not return a prompt_id for the queued workflow.",
            )
        return prompt_id

    @staticmethod
    def _describe_rejection(data: Any) -> str:
        """Short summary of a ComfyUI ``error`` and ``node_errors`` body."""
        if not isinstance(data, dict):
            return "no details"
        parts: list[str] = []
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                parts.append(message)
        elif isinstance(error, str) and error:
            parts.append(error)
        node_errors = data.get("node_errors")
        if isinstance(node_errors, dict):
            for node_id in sorted(node_errors):
                node = node_errors[node_id]
                if not isinstance(node, dict):
                    continue
                class_type = node.get("class_type", "node")
                messages = [
                    str(item.get("message", ""))
                    for item in node.get("errors", [])
                    if isinstance(item, dict)
                ]
                parts.append(f"{class_type}: {', '.join(m for m in messages if m)}")
        return _summary("; ".join(parts)) if parts else "no details"

    def _wait_for_job(self, prompt_id: str) -> tuple[dict[str, Any], int]:
        """Poll ``/history/{prompt_id}`` until the job is done (step 3)."""
        path = f"/history/{urllib.parse.quote(prompt_id, safe='')}"
        deadline = self._clock() + self.job_timeout_seconds
        polls = 0
        transient = 0
        while True:
            polls += 1
            state, payload = self._poll_once(path, prompt_id)
            if state == "done":
                return payload, polls
            if state == "transient":
                transient += 1
                if transient > self.max_transient_retries:
                    raise self._error(
                        ImageGeneratorErrorCode.UNAVAILABLE,
                        f"ComfyUI polling failed {transient} times in a row "
                        f"for job {prompt_id}: {payload}",
                    )
            else:
                transient = 0
            if self._clock() >= deadline:
                raise self._error(
                    ImageGeneratorErrorCode.UNAVAILABLE,
                    f"timed out after {self.job_timeout_seconds:g}s waiting "
                    f"for job {prompt_id}",
                )
            self._sleep(self.poll_interval_seconds)

    def _poll_once(self, path: str, prompt_id: str) -> tuple[str, Any]:
        """Return ("pending" | "transient" | "done", entry or detail)."""
        try:
            response = self._request("GET", path)
        except ComfyTransportError as exc:
            return "transient", str(exc)
        if response.status == 404:
            return "pending", None
        if response.status == 429 or response.status >= 500:
            return "transient", f"HTTP {response.status}"
        if response.status != 200:
            raise self._status_error(response.status, "the job history")
        history = _json_or_none(response.body)
        if not isinstance(history, dict):
            return "transient", "unparsable history response"
        entry = history.get(prompt_id)
        if not isinstance(entry, dict):
            return "pending", None
        status = entry.get("status")
        status = status if isinstance(status, dict) else {}
        status_str = status.get("status_str")
        if status_str == "error":
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                f"ComfyUI job {prompt_id} failed: {self._execution_error(status)}",
            )
        outputs = entry.get("outputs")
        if (status.get("completed") is True or status_str == "success") and isinstance(
            outputs, dict
        ):
            return "done", entry
        return "pending", None

    @staticmethod
    def _execution_error(status: dict[str, Any]) -> str:
        messages = status.get("messages")
        if isinstance(messages, list):
            for item in messages:
                if (
                    isinstance(item, list | tuple)
                    and len(item) == 2
                    and item[0] == "execution_error"
                    and isinstance(item[1], dict)
                ):
                    info = item[1]
                    node_type = info.get("node_type", "unknown node")
                    message = info.get("exception_message", "no message")
                    return _summary(f"{node_type}: {message}")
        return "execution error"

    def _pick_output_image(self, entry: dict[str, Any]) -> tuple[str, str, str]:
        """Choose the SaveImage output to download (step 4)."""
        outputs = entry.get("outputs")
        outputs = outputs if isinstance(outputs, dict) else {}

        def node_order(node_id: str) -> tuple[int, int, str]:
            return (node_id != "9", int(node_id) if node_id.isdigit() else 0, node_id)

        candidates: list[tuple[str, str, str]] = []
        for node_id in sorted(outputs, key=node_order):
            node = outputs[node_id]
            images = node.get("images") if isinstance(node, dict) else None
            if not isinstance(images, list):
                continue
            for image in images:
                if not isinstance(image, dict):
                    continue
                filename = image.get("filename")
                if not isinstance(filename, str) or not filename:
                    continue
                subfolder = image.get("subfolder")
                image_type = image.get("type")
                candidates.append(
                    (
                        filename,
                        subfolder if isinstance(subfolder, str) else "",
                        image_type if isinstance(image_type, str) else "output",
                    )
                )
        if not candidates:
            raise self._error(
                ImageGeneratorErrorCode.GENERATION_FAILED,
                "job completed without images",
            )
        for candidate in candidates:
            if candidate[2] == "output":
                return candidate
        return candidates[0]

    def _download_image(self, filename: str, subfolder: str, image_type: str) -> bytes:
        """GET ``/view`` for the chosen image (step 5)."""
        query = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": image_type},
            quote_via=urllib.parse.quote,
        )
        failures = 0
        while True:
            try:
                response = self._request("GET", f"/view?{query}")
            except ComfyTransportError as exc:
                detail = str(exc)
            else:
                if response.status == 200:
                    return response.body
                if response.status == 404:
                    raise self._error(
                        ImageGeneratorErrorCode.GENERATION_FAILED,
                        "ComfyUI no longer has the output image (HTTP 404).",
                    )
                if response.status != 429 and response.status < 500:
                    raise self._status_error(response.status, "the output image")
                detail = f"HTTP {response.status}"
            failures += 1
            if failures > self.max_transient_retries:
                raise self._error(
                    ImageGeneratorErrorCode.UNAVAILABLE,
                    f"downloading the ComfyUI output failed {failures} times "
                    f"in a row: {detail}",
                )
            self._sleep(self.poll_interval_seconds)

    def _build_prompt_graph(
        self, req: ImageGenerationRequest, seed: int
    ) -> dict[str, Any]:
        """Construct a standard ComfyUI SDXL/SD text2image workflow graph."""
        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "cfg": 7.0,
                    "denoise": 1.0,
                    "latent_image": ["5", 0],
                    "model": ["4", 0],
                    "negative": ["7", 0],
                    "positive": ["6", 0],
                    "sampler_name": "euler_ancestral",
                    "scheduler": "normal",
                    "seed": seed,
                    "steps": 20,
                },
            },
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": self.default_model},
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "batch_size": 1,
                    "height": req.height,
                    "width": req.width,
                },
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "clip": ["4", 1],
                    "text": f"{req.prompt}, {req.style}, masterpiece, 8k",
                },
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "clip": ["4", 1],
                    "text": (
                        "ugly, blurry, lowres, deformed, bad anatomy, text, watermark"
                    ),
                },
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": "s2v_"
                    + _PREFIX_UNSAFE.sub("_", req.scene_id)[:64],
                    "images": ["8", 0],
                },
            },
        }

    # Satisfy ImageGenerator protocol capabilities()
    def __getattr__(self, name: str) -> Any:
        if name == "capabilities" and hasattr(self, "capabilities_image_gen"):
            # Provide compatibility when called as ImageGenerator
            return self.capabilities_image_gen
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )
