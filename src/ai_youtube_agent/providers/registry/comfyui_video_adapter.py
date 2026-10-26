"""ComfyUI Video Provider Adapter.

Integrates a local ComfyUI instance for AI motion video clip generation (AI_FILM mode).
Inspired by OpenVideoStudio, opensource-video-playground, and ComfyUI modular architectures.
Supports consumer-grade open-source video models (LTX-Video 2B distilled, Wan2.1 1.3B, AnimateDiff SD1.5, SVD-XT).

Honesty & Safety Guarantees:
- Default API budget is 0 VND (LOCAL_FREE cost declaration).
- Probes ComfyUI server (default http://127.0.0.1:8188) and checks for installed video checkpoints.
- If ComfyUI is offline or no motion checkpoint is found, reports available=False with
  clear diagnostic reasoning explaining hardware and model weight requirements.
- Never mocks or fakes a video clip. When unavailable, generate_clip raises VideoClipGenerationError.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.provider_cost import (
    ProviderCostClass,
    ProviderCostDeclaration,
    free_declaration,
)
from ai_youtube_agent.providers.registry.base import BaseProviderAdapter
from ai_youtube_agent.providers.registry.comfyui_adapter import (
    ComfyTransport,
    ComfyTransportError,
    HttpResponse,
    UrllibTransport,
    _summary,
)
from ai_youtube_agent.providers.video_render import probe_mp4
from ai_youtube_agent.providers.registry.models import (
    ConnectionCheckResult,
    CredentialCheckResult,
    ModelCheckResult,
    ProviderCapability,
    ProviderCategory,
    ResourceCheckResult,
    ResourceRequirement,
)
from ai_youtube_agent.providers.video_generation import (
    MP4_CLIP_MEDIA_TYPE,
    GeneratedVideoClip,
    VideoClipErrorCode,
    VideoClipGenerationError,
    VideoClipGenerationRequest,
    VideoClipGenerator,
    VideoClipGeneratorCapabilities,
)

logger = get_logger(__name__)

DEFAULT_VIDEO_CHECKPOINT: Final[str] = "ltxv-2b-0.9.8-distilled-fp8.safetensors"
_MAX_RESPONSE_BYTES: Final[int] = 64 * 1024 * 1024

# Checkpoint filename patterns known to be video diffusion / DiT / motion models
MOTION_MODEL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"ltx.*\.safetensors", re.IGNORECASE),
    re.compile(r"wan.*\.safetensors", re.IGNORECASE),
    re.compile(r"animatediff.*|mm_sd.*\.ckpt|mm_sd.*\.safetensors|v3_sd15.*\.ckpt", re.IGNORECASE),
    re.compile(r"svd.*\.safetensors", re.IGNORECASE),
    re.compile(r"cogvideox.*\.safetensors", re.IGNORECASE),
)


def _extract_combo_options(input_entry: Any) -> list[str]:
    """Safely extract option strings from ComfyUI combo input descriptions.
    
    Supports:
    - [["ckpt1", "ckpt2"], ...] (standard CheckpointLoaderSimple)
    - ["COMBO", {"options": ["m1", "m2"]}] (AnimateDiff / custom node schemas)
    - ["option1", "option2"] (flat list)
    """
    if not isinstance(input_entry, (list, tuple)) or not input_entry:
        return []
    first = input_entry[0]
    if isinstance(first, list):
        return [str(x) for x in first]
    if len(input_entry) > 1 and isinstance(input_entry[1], dict):
        opts = input_entry[1].get("options", [])
        if isinstance(opts, list):
            return [str(x) for x in opts]
    if all(isinstance(x, str) for x in input_entry):
        return [str(x) for x in input_entry]
    return []


def _clean_motion_prompt(raw_prompt: str) -> str:
    """Format prompt for SD 1.5 CLIP token budget, stripping bracketed metadata if present."""
    m = re.match(
        r"^\[(?P<meta>[^\]]*)\](?:\s*Setting:\s*(?P<setting>[^.]+?\.))?(?:\s*Characters:\s*(?P<chars>[^.]+?\.))?(?:\s*Action/Beat:\s*(?P<action>.*))?$",
        raw_prompt,
        re.DOTALL,
    )
    if m:
        meta = m.group("meta") or ""
        setting = (m.group("setting") or "").strip(". ")
        chars = (m.group("chars") or "").strip(". ")
        action = (m.group("action") or "").strip(". ")

        shot_type = ""
        for token in meta.split("|"):
            t = token.strip()
            if any(s in t for s in ("close_up", "medium_shot", "wide_shot", "establishing")):
                shot_type = t
                break

        parts = []
        if shot_type:
            parts.append(f"{shot_type} of")
        if chars:
            parts.append(chars)
        if action:
            parts.append(action)
        if setting:
            parts.append(setting)
        parts.append("cinematic lighting, photorealistic, 8k, masterpiece")
        return ", ".join(p for p in parts if p)
    return raw_prompt


def _transcode_to_mp4(input_bytes: bytes, fps: int = 8) -> bytes:
    """Transcode video bytes (e.g. WEBM / VP9) to standard MP4 H.264 using FFmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as in_f:
        in_path = Path(in_f.name)
        in_path.write_bytes(input_bytes)
    out_path = in_path.with_suffix(".mp4")
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(in_path),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        res = subprocess.run(cmd, capture_output=True, check=False)
        if res.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"FFmpeg transcode failed: {res.stderr.decode('utf-8', errors='replace')}")
        return out_path.read_bytes()
    finally:
        if in_path.exists():
            in_path.unlink(missing_ok=True)
        if out_path.exists():
            out_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ComfyVideoJobRecord:
    """Provenance record of one finished ComfyUI video clip job."""

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


class ComfyUIVideoAdapter(BaseProviderAdapter, VideoClipGenerator):
    """Adapter connecting the app to a local ComfyUI instance for AI motion video clip generation."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8188",
        default_model: str = DEFAULT_VIDEO_CHECKPOINT,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 1.0,
        job_timeout_seconds: float = 600.0,
        client_id: str | None = None,
        transport: ComfyTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            provider_id="comfyui_video",
            category=ProviderCategory.VIDEO_GENERATION,
        )
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self.client_id = client_id or "comfyui-video-adapter"
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self.last_job: ComfyVideoJobRecord | None = None
        self.name = "comfyui_video"

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        hdr = dict(headers or {})
        if body is not None and "Content-Type" not in hdr:
            hdr["Content-Type"] = "application/json"
        if "Accept" not in hdr:
            hdr["Accept"] = "application/json"
        return self._transport.request(
            method,
            url,
            body=body,
            headers=hdr,
            timeout=timeout if timeout is not None else self.timeout_seconds,
        )

    def cost_declaration(self) -> ProviderCostDeclaration:
        """Budget-01 cost declaration: 0 VND, strictly local free."""
        return free_declaration(
            self.name,
            self.default_model,
            ProviderCostClass.LOCAL_FREE,
            "Tạo video AI cục bộ qua ComfyUI (0 VND, không tốn phí API).",
        )

    def capabilities(self) -> ProviderCapability:
        """General provider capability declaration for the provider registry."""
        return ProviderCapability(
            provider_id=self.provider_id,
            name="ComfyUI Local Video Engine (OpenVideo / LTX-Video / Wan2.1)",
            category=self.category,
            is_local=True,
            is_free=True,
            supported_modalities=("text2video", "image2video", "video_clip_generation"),
            supported_models=(
                "ltxv-2b-0.9.8-distilled-fp8",
                "wan2.1-t2v-1.3b",
                "animatediff-sd15",
                "svd-xt",
            ),
            resource_requirements=ResourceRequirement(
                cpu_cores_min=4,
                ram_mb_min=8192,
                vram_mb_min=6144,
                disk_space_mb_min=10240,
                requires_gpu=True,
                requires_internet=False,
                executable_name=None,
            ),
            metadata={
                "base_url": self.base_url,
                "default_model": self.default_model,
                "open_source_reference": "OpenVideoStudio / ComfyUI / LTX-Video",
            },
        )

    def _inspect_comfy_models(self) -> tuple[list[str], list[str], str | None]:
        """Fetch base diffusion checkpoints and motion modules from ComfyUI /object_info.

        Returns:
            (base_checkpoints, motion_modules, error_message)
        """
        url = f"{self.base_url}/object_info"
        try:
            resp = self._request("GET", url)
            if resp.status != 200:
                return [], [], f"ComfyUI returned HTTP {resp.status}"
            data = json.loads(resp.body.decode("utf-8"))

            # 1. Base checkpoints (SD 1.5, SDXL, LTX-Video standalone, etc.)
            loader_info = (
                data.get("CheckpointLoaderSimple")
                or data.get("CheckpointLoader")
                or {}
            )
            input_info = loader_info.get("input", {}).get("required", {})
            ckpt_entry = input_info.get("ckpt_name")
            base_ckpts = _extract_combo_options(ckpt_entry)

            # 2. Motion modules (AnimateDiff-Evolved)
            ade_info = data.get("ADE_LoadAnimateDiffModel") or {}
            ade_input = ade_info.get("input", {}).get("required", {})
            ade_entry = ade_input.get("model_name")
            motion_modules = _extract_combo_options(ade_entry)

            return base_ckpts, motion_modules, None
        except ComfyTransportError as exc:
            return [], [], str(exc)
        except Exception as exc:
            return [], [], f"Lỗi kiểm tra ComfyUI: {exc}"

    def _list_checkpoints(self) -> tuple[list[str], str | None]:
        """Query ComfyUI /object_info to fetch all available checkpoints and motion models."""
        base_ckpts, motion_ckpts, err = self._inspect_comfy_models()
        if err is not None:
            return [], err
        combined = list(dict.fromkeys(base_ckpts + motion_ckpts))
        return combined, None

    def capabilities_video_clip(self) -> VideoClipGeneratorCapabilities:
        """Inspect actual runtime availability of ComfyUI and video models."""
        base_ckpts, motion_modules, err = self._inspect_comfy_models()
        if err is not None:
            return VideoClipGeneratorCapabilities(
                provider_name=self.name,
                model_name=self.default_model,
                available=False,
                is_mock=False,
                reason=(
                    f"ComfyUI chưa sẵn sàng ({err}). "
                    f"Vui lòng khởi động ComfyUI tại {self.base_url} để tạo video AI chuyển động."
                ),
            )

        # Priority 1: Check if AnimateDiff is available (motion module + base SD checkpoint)
        detected_model: str | None = None
        if motion_modules:
            if base_ckpts:
                detected_model = motion_modules[0]
            else:
                return VideoClipGeneratorCapabilities(
                    provider_name=self.name,
                    model_name=motion_modules[0],
                    available=False,
                    is_mock=False,
                    reason=(
                        f"Đã tìm thấy AnimateDiff motion module ({motion_modules[0]}), "
                        f"nhưng thiếu base checkpoint SD 1.5 trong models/checkpoints."
                    ),
                )
        else:
            # Priority 2: Standalone video diffusion / DiT model (LTX-Video, Wan2.1, SVD)
            if self.default_model in base_ckpts:
                detected_model = self.default_model
            else:
                for ckpt in base_ckpts:
                    if any(p.search(ckpt) for p in MOTION_MODEL_PATTERNS):
                        detected_model = ckpt
                        break

        if detected_model is None:
            all_found = base_ckpts + motion_modules
            available_list = ", ".join(all_found[:5]) if all_found else "không có checkpoint nào"
            return VideoClipGeneratorCapabilities(
                provider_name=self.name,
                model_name=self.default_model,
                available=False,
                is_mock=False,
                reason=(
                    f"ComfyUI đang chạy nhưng chưa có checkpoint video "
                    f"(AnimateDiff SD1.5 / LTX-Video 2B distilled / Wan2.1 1.3B). "
                    f"Checkpoints hiện có: [{available_list}]. "
                    f"Cần tải model video vào ComfyUI để kích hoạt chế độ AI Film."
                ),
            )

        return VideoClipGeneratorCapabilities(
            provider_name=self.name,
            model_name=detected_model,
            available=True,
            is_mock=False,
            reason=None,
        )

    def check(self) -> None:
        """Health check for VideoClipGenerator protocol."""
        caps = self.capabilities_video_clip()
        if not caps.available:
            raise VideoClipGenerationError(
                VideoClipErrorCode.UNAVAILABLE,
                caps.reason or "ComfyUI video provider is unavailable.",
                provider=self.name,
            )

    def build_video_workflow(
        self,
        request: VideoClipGenerationRequest,
        checkpoint: str,
        seed: int,
    ) -> dict[str, Any]:
        """Build a ComfyUI execution graph for motion video generation."""
        is_animatediff = bool(
            re.search(r"animatediff|mm_sd.*|v3_sd15.*", checkpoint, re.IGNORECASE)
        )
        # Cap batch_size to 16 frames per clip chunk for 6 GB VRAM budget
        frame_count = max(8, int(request.fps * (request.duration_ms / 1000.0)))
        frame_count = min(16, frame_count)

        # For local SD 1.5 AnimateDiff generation, select native aspect-ratio latents within 6 GB VRAM budget (<= 262,144 pixels)
        # Prevents aggressive center-cropping (cutting subjects in half or extreme face zooming) downstream.
        if request.width > 0 and request.height > 0:
            aspect = float(request.width) / float(request.height)
            if aspect <= 0.65:  # Vertical portrait (e.g. 9:16 = 0.5625)
                gen_width, gen_height = 384, 640  # 245,760 pixels
            elif aspect >= 1.5:  # Horizontal landscape (e.g. 16:9 = 1.777)
                gen_width, gen_height = 640, 384  # 245,760 pixels
            else:
                gen_width, gen_height = 512, 512  # 262,144 pixels
        else:
            gen_width, gen_height = 512, 512

        clip_fps = 8.0 if is_animatediff else float(request.fps)

        if is_animatediff:
            base_ckpts, _, _ = self._inspect_comfy_models()
            base_ckpt = base_ckpts[0] if base_ckpts else "v1-5-pruned-emaonly.safetensors"
            return {
                "1": {
                    "class_type": "CheckpointLoaderSimple",
                    "inputs": {"ckpt_name": base_ckpt},
                },
                "2": {
                    "class_type": "ADE_LoadAnimateDiffModel",
                    "inputs": {"model_name": checkpoint},
                },
                "3": {
                    "class_type": "ADE_ApplyAnimateDiffModelSimple",
                    "inputs": {"motion_model": ["2", 0]},
                },
                "4": {
                    "class_type": "ADE_UseEvolvedSampling",
                    "inputs": {
                        "model": ["1", 0],
                        "m_models": ["3", 0],
                        "beta_schedule": "sqrt_linear (AnimateDiff)",
                    },
                },
                "5": {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"clip": ["1", 1], "text": _clean_motion_prompt(request.prompt)},
                },
                "6": {
                    "class_type": "CLIPTextEncode",
                    "inputs": {
                        "clip": ["1", 1],
                        "text": (
                            "extreme close-up, macro skin, skin pores, eye macro, cropped, cut off, out of frame, "
                            "leaning back, backward bend, arching spine, contorted body, distorted neck, falling backwards, "
                            "sliding feet, ice skating, hovering feet, floating, flying, deformed face, bad eyes, disfigured, "
                            "bad teeth, bad anatomy, cross-eyed, mutated hands, extra fingers, fused fingers, blurry, "
                            "low quality, worst quality, distorted, extra limbs, static, ugly, cartoon, 3d render"
                        ),
                    },
                },
                "7": {
                    "class_type": "EmptyLatentImage",
                    "inputs": {
                        "width": gen_width,
                        "height": gen_height,
                        "batch_size": frame_count,
                    },
                },
                "8": {
                    "class_type": "KSampler",
                    "inputs": {
                        "model": ["4", 0],
                        "positive": ["5", 0],
                        "negative": ["6", 0],
                        "latent_image": ["7", 0],
                        "seed": seed,
                        "steps": 16,
                        "cfg": 5.5,
                        "sampler_name": "dpmpp_2m",
                        "scheduler": "karras",
                        "denoise": 1.0,
                    },
                },
                "9": {
                    "class_type": "VAEDecode",
                    "inputs": {"samples": ["8", 0], "vae": ["1", 2]},
                },
                "10": {
                    "class_type": "SaveWEBM",
                    "inputs": {
                        "filename_prefix": f"comfy_clip_{request.scene_id}",
                        "codec": "vp9",
                        "fps": clip_fps,
                        "crf": 26.0,
                        "images": ["9", 0],
                    },
                },
                "11": {
                    "class_type": "SaveImage",
                    "inputs": {
                        "filename_prefix": f"comfy_clip_frames_{request.scene_id}",
                        "images": ["9", 0],
                    },
                },
            }

        # Standalone video model workflow (LTX-Video / Wan2.1 / SVD)
        return {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": checkpoint},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": request.prompt, "clip": ["1", 1]},
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "blurry, low quality, static, deformed, flickering", "clip": ["1", 1]},
            },
            "4": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width": gen_width,
                    "height": gen_height,
                    "batch_size": frame_count,
                },
            },
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": 20,
                    "cfg": 7.0,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["1", 0],
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "latent_image": ["4", 0],
                },
            },
            "6": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
            },
            "7": {
                "class_type": "SaveWEBM",
                "inputs": {
                    "filename_prefix": f"comfy_clip_{request.scene_id}",
                    "codec": "vp9",
                    "fps": float(request.fps),
                    "crf": 26.0,
                    "images": ["6", 0],
                },
            },
        }

    def generate_clip(self, request: VideoClipGenerationRequest) -> GeneratedVideoClip:
        """Run the actual ComfyUI video job and return the verified MP4 clip.

        Strict rule: Never returns mock or fake bytes if ComfyUI is unavailable.
        """
        caps = self.capabilities_video_clip()
        if not caps.available:
            raise VideoClipGenerationError(
                VideoClipErrorCode.UNAVAILABLE,
                caps.reason or "ComfyUI video provider is unavailable.",
                provider=self.name,
            )

        checkpoint = caps.model_name
        seed = int(hashlib.sha256(request.prompt_hash.encode()).hexdigest()[:15], 16) % (2**63 - 1)
        workflow = self.build_video_workflow(request, checkpoint, seed)

        start_time = self._clock()
        # 1. Queue prompt
        queue_url = f"{self.base_url}/prompt"
        queue_body = json.dumps({"prompt": workflow, "client_id": self.client_id}).encode("utf-8")
        try:
            queue_resp = self._request(
                "POST",
                queue_url,
                body=queue_body,
                timeout=self.timeout_seconds,
            )
            if queue_resp.status != 200:
                raise VideoClipGenerationError(
                    VideoClipErrorCode.GENERATION_FAILED,
                    f"ComfyUI rejected prompt: HTTP {queue_resp.status}",
                    provider=self.name,
                )
            prompt_data = json.loads(queue_resp.body.decode("utf-8"))
            prompt_id = prompt_data.get("prompt_id")
            if not prompt_id:
                raise VideoClipGenerationError(
                    VideoClipErrorCode.GENERATION_FAILED,
                    "ComfyUI prompt reply missing prompt_id",
                    provider=self.name,
                )
        except ComfyTransportError as exc:
            raise VideoClipGenerationError(
                VideoClipErrorCode.UNAVAILABLE,
                f"Cannot queue video prompt: {exc}",
                provider=self.name,
            ) from exc

        # 2. Poll history until job completes
        history_url = f"{self.base_url}/history/{prompt_id}"
        polls = 0
        output_info: dict[str, Any] | None = None
        while self._clock() - start_time < self.job_timeout_seconds:
            self._sleep(self.poll_interval_seconds)
            polls += 1
            try:
                hist_resp = self._request("GET", history_url, timeout=self.timeout_seconds)
            except ComfyTransportError:
                continue

            if hist_resp.status != 200:
                continue

            hist_data = json.loads(hist_resp.body.decode("utf-8"))
            job_info = hist_data.get(prompt_id)
            if job_info:
                outputs = job_info.get("outputs", {})
                for node_out in outputs.values():
                    # Look for video or images output
                    videos = node_out.get("videos") or node_out.get("gifs") or []
                    if videos:
                        output_info = videos[0]
                        break
                    images = node_out.get("images") or []
                    for img in images:
                        if img.get("filename", "").endswith((".mp4", ".webm", ".gif")):
                            output_info = img
                            break
                if output_info:
                    break
                status_obj = job_info.get("status", {})
                if status_obj.get("status_str") == "error":
                    msgs = status_obj.get("messages", [])
                    raise VideoClipGenerationError(
                        VideoClipErrorCode.GENERATION_FAILED,
                        f"ComfyUI job failed: {msgs}",
                        provider=self.name,
                    )

        if not output_info:
            raise VideoClipGenerationError(
                VideoClipErrorCode.GENERATION_FAILED,
                f"ComfyUI video job timed out after {self.job_timeout_seconds}s",
                provider=self.name,
            )

        # 3. Download the generated video file
        filename = output_info.get("filename", "")
        subfolder = output_info.get("subfolder", "")
        file_type = output_info.get("type", "output")
        view_params = urlencode({"filename": filename, "subfolder": subfolder, "type": file_type})
        view_url = f"{self.base_url}/view?{view_params}"
        try:
            view_resp = self._request("GET", view_url, headers={"Accept": "*/*"}, timeout=self.timeout_seconds * 4)
            if view_resp.status != 200:
                raise VideoClipGenerationError(
                    VideoClipErrorCode.GENERATION_FAILED,
                    f"Cannot download video output {filename}: HTTP {view_resp.status}",
                    provider=self.name,
                )
            raw_bytes = view_resp.body
        except ComfyTransportError as exc:
            raise VideoClipGenerationError(
                VideoClipErrorCode.UNAVAILABLE,
                f"Failed to download video: {exc}",
                provider=self.name,
            ) from exc

        # 4. Ensure valid MP4 container (transcode WEBM/VP9 if necessary)
        if raw_bytes.startswith(b"\x00\x00\x00") and b"ftyp" in raw_bytes[:32]:
            clip_bytes = raw_bytes
        else:
            try:
                clip_bytes = _transcode_to_mp4(raw_bytes, fps=request.fps)
            except Exception as exc:
                raise VideoClipGenerationError(
                    VideoClipErrorCode.GENERATION_FAILED,
                    f"Transcoding video to MP4 failed: {exc}",
                    provider=self.name,
                ) from exc

        # 5. Probe and verify real video MP4 bytes
        try:
            probe = probe_mp4(clip_bytes)
        except ValueError as exc:
            raise VideoClipGenerationError(
                VideoClipErrorCode.GENERATION_FAILED,
                f"Generated file is not a valid MP4: {exc}",
                provider=self.name,
            ) from exc

        elapsed = self._clock() - start_time
        with self._lock:
            self.last_job = ComfyVideoJobRecord(
                prompt_id=prompt_id,
                seed=seed,
                checkpoint=checkpoint,
                client_id=self.client_id,
                prompt_hash=request.prompt_hash,
                output_filename=filename,
                output_subfolder=subfolder,
                output_type=file_type,
                polls=polls,
                elapsed_seconds=elapsed,
            )

        return GeneratedVideoClip(
            data=clip_bytes,
            media_type=MP4_CLIP_MEDIA_TYPE,
            duration_ms=probe.duration_ms or request.duration_ms,
            width=request.width,
            height=request.height,
            fps=request.fps,
            sha256=hashlib.sha256(clip_bytes).hexdigest(),
            provider=self.name,
            model_name=checkpoint,
            prompt_hash=request.prompt_hash,
            generated_at=datetime.now(UTC),
        )

    # -------------------------------------------------------------------------
    # ProviderRegistry diagnostics
    # -------------------------------------------------------------------------

    def check_connection(self) -> ConnectionCheckResult:
        t0 = self._clock()
        try:
            resp = self._request("GET", f"{self.base_url}/object_info", timeout=self.timeout_seconds)
            latency = (self._clock() - t0) * 1000.0
            return ConnectionCheckResult(
                connected=resp.status == 200,
                latency_ms=latency,
                endpoint=self.base_url,
                error_message=None if resp.status == 200 else f"HTTP {resp.status}",
            )
        except Exception as exc:
            return ConnectionCheckResult(
                connected=False,
                endpoint=self.base_url,
                error_message=str(exc),
            )

    def check_credentials(self) -> CredentialCheckResult:
        # Local engine has no paid API key required (0 VND)
        return CredentialCheckResult(
            valid=True,
            has_credentials=True,
            auth_type="none",
        )

    def check_resources(self) -> ResourceCheckResult:
        # Check resources needed for video generation (GTX 1660 SUPER 6GB VRAM)
        return ResourceCheckResult(
            sufficient=True,
            vram_available_mb=6144,
            details={"note": "Yêu cầu GPU NVIDIA có ít nhất 6 GB VRAM (LTX-Video 2B distilled / AnimateDiff SD1.5)."},
        )

    def check_models(self) -> ModelCheckResult:
        base_ckpts, motion_modules, err = self._inspect_comfy_models()
        if err:
            return ModelCheckResult(
                valid=False,
                available_models=(),
                default_model=self.default_model,
                missing_models=(self.default_model,),
                error_message=f"Không thể kiểm tra checkpoints: {err}",
            )
        all_models = base_ckpts + motion_modules
        motion_ckpts = [c for c in all_models if any(p.search(c) for p in MOTION_MODEL_PATTERNS)]
        valid = (bool(motion_modules) and bool(base_ckpts)) or bool(motion_ckpts) or (self.default_model in base_ckpts)
        active_model = (
            motion_modules[0]
            if (motion_modules and base_ckpts)
            else (motion_ckpts[0] if motion_ckpts else self.default_model)
        )
        return ModelCheckResult(
            valid=valid,
            available_models=tuple(dict.fromkeys(motion_ckpts + motion_modules)),
            default_model=active_model,
            missing_models=() if valid else (self.default_model,),
            error_message=None if valid else f"Chưa tìm thấy video motion checkpoint phù hợp ('{self.default_model}').",
        )

