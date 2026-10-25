"""Backgrounds: AI plates from a local ComfyUI server with a VRAM ladder.

Each storyboard location becomes one 1080x1920 plate made in two passes (an
SD 1.5 text2image pass at 576x1024, then an img2img hires fix with denoise 0.5).
If the card runs out of memory the job is retried one tier down:

* A: pass 2 at 1080x1920 with the normal VAE nodes;
* B: the same with explicit tiled VAE encode and decode;
* C: pass 2 at 810x1440 (tiled VAE), then a Lanczos 1.333x resize to 1080x1920,
  labelled in the record as a resize, not a diffusion pass.

When every tier fails the step fails: no placeholder image is ever made.
Plates are cached on disk by prompt, seed, checkpoint and plan.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.storyboard import LocationSpec, Storyboard
from ai_youtube_agent.providers.image_generation import (
    ImageGenerationError,
    ImageGenerationRequest,
)
from ai_youtube_agent.providers.media_probe import MediaProbeError, probe_image_bytes

CACHE_VERSION: Final[str] = "bg-v1"
JOB_TIMEOUT_S: Final[float] = 900.0
POST_RESIZE_LABEL: Final[str] = "lanczos 1.333x, not a diffusion pass"
_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_OOM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"out of memory|allocation on device|\boom\b", re.IGNORECASE
)
_BLANK_ZERO_FRACTION: Final[float] = 0.995
_BLANK_MIN_DISTINCT_BYTES: Final[int] = 8


class BackgroundTier(StrEnum):
    A = "A"
    B = "B"
    C = "C"


TIER_ORDER: Final[tuple[BackgroundTier, ...]] = (
    BackgroundTier.A,
    BackgroundTier.B,
    BackgroundTier.C,
)


@dataclass(frozen=True)
class TwoPassPlan:
    """Sizes and sampler settings of the two-pass hires fix."""

    pass1_size: tuple[int, int] = (576, 1024)
    pass1_steps: int = 24
    pass1_cfg: float = 7.0
    pass2_size: tuple[int, int] = (1080, 1920)
    pass2_steps: int = 20
    pass2_cfg: float = 7.0
    pass2_denoise: float = 0.5
    sampler: str = "euler_ancestral"
    scheduler: str = "normal"
    final_size: tuple[int, int] = (1080, 1920)
    tile_size: int = 512
    tile_overlap: int = 64

    @property
    def post_resize(self) -> str | None:
        return POST_RESIZE_LABEL if self.pass2_size != self.final_size else None


def plan_for_tier(tier: BackgroundTier, base: TwoPassPlan | None = None) -> TwoPassPlan:
    """The plan of ``tier``: C renders pass 2 smaller (the 1.333x resize follows)."""
    plan = base or TwoPassPlan()
    if tier is BackgroundTier.C:
        width, height = plan.final_size
        return replace(plan, pass2_size=(round(width * 0.75), round(height * 0.75)))
    return replace(plan, pass2_size=plan.final_size)


def tier_uses_tiled_vae(tier: BackgroundTier) -> bool:
    return tier is not BackgroundTier.A


def build_two_pass_graph(
    plan: TwoPassPlan,
    positive: str,
    negative: str,
    seed: int,
    checkpoint: str,
    tiled: bool,
    *,
    filename_prefix: str = "anim_bg",
) -> dict[str, Any]:
    """The ComfyUI workflow of the two-pass hires fix (API format).

    CheckpointLoaderSimple -> CLIPTextEncode x2 -> EmptyLatentImage -> KSampler
    -> VAEDecode -> ImageScale (lanczos) -> VAEEncode[Tiled] -> KSampler
    (denoise) -> VAEDecode[Tiled] -> [ImageScale back to the final size] ->
    SaveImage. The SaveImage node is "9" like in the adapter's default graph.
    """
    width1, height1 = plan.pass1_size
    width2, height2 = plan.pass2_size
    tile = {
        "tile_size": plan.tile_size,
        "overlap": plan.tile_overlap,
        "temporal_size": 64,
        "temporal_overlap": 8,
    }
    graph: dict[str, Any] = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": positive},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": negative},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"batch_size": 1, "height": height1, "width": width1},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "cfg": plan.pass1_cfg,
                "denoise": 1.0,
                "latent_image": ["5", 0],
                "model": ["4", 0],
                "negative": ["7", 0],
                "positive": ["6", 0],
                "sampler_name": plan.sampler,
                "scheduler": plan.scheduler,
                "seed": seed,
                "steps": plan.pass1_steps,
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "10": {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["8", 0],
                "upscale_method": "lanczos",
                "width": width2,
                "height": height2,
                "crop": "disabled",
            },
        },
        "11": {
            "class_type": "VAEEncodeTiled" if tiled else "VAEEncode",
            "inputs": {"pixels": ["10", 0], "vae": ["4", 2], **(tile if tiled else {})},
        },
        "12": {
            "class_type": "KSampler",
            "inputs": {
                "cfg": plan.pass2_cfg,
                "denoise": plan.pass2_denoise,
                "latent_image": ["11", 0],
                "model": ["4", 0],
                "negative": ["7", 0],
                "positive": ["6", 0],
                "sampler_name": plan.sampler,
                "scheduler": plan.scheduler,
                "seed": seed,
                "steps": plan.pass2_steps,
            },
        },
        "13": {
            "class_type": "VAEDecodeTiled" if tiled else "VAEDecode",
            "inputs": {
                "samples": ["12", 0],
                "vae": ["4", 2],
                **(tile if tiled else {}),
            },
        },
    }
    last = "13"
    if plan.post_resize is not None:
        graph["14"] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["13", 0],
                "upscale_method": "lanczos",
                "width": plan.final_size[0],
                "height": plan.final_size[1],
                "crop": "disabled",
            },
        }
        last = "14"
    graph["9"] = {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": filename_prefix, "images": [last, 0]},
    }
    return graph


# ---------------------------------------------------------------------------
# PNG checks
# ---------------------------------------------------------------------------


def png_is_blank(data: bytes) -> bool:
    """True when the PNG pixels carry (almost) no information.

    The filtered scanlines of a flat image are all zero bytes but the first of
    each row; a drawn plate has detail everywhere. Pure Python (zlib only).
    """
    if not data.startswith(_PNG_SIGNATURE):
        raise MediaProbeError("not a PNG")
    position = len(_PNG_SIGNATURE)
    pixels = bytearray()
    while position + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, position)
        kind = data[position + 4 : position + 8]
        if kind == b"IDAT":
            pixels += data[position + 8 : position + 8 + length]
        position += 12 + length
        if kind == b"IEND":
            break
    try:
        raw = zlib.decompress(bytes(pixels))
    except zlib.error as exc:
        raise MediaProbeError(f"PNG pixel data is corrupt: {exc}") from exc
    if not raw:
        return True
    # Flat colour: all zeros after a Sub/Up/Paeth filter, or very few distinct
    # byte values when the encoder used no filter at all.
    return (
        raw.count(0) / len(raw) >= _BLANK_ZERO_FRACTION
        or len(set(raw)) < _BLANK_MIN_DISTINCT_BYTES
    )


def validate_plate(data: bytes, size: tuple[int, int]) -> None:
    """The plate is a complete PNG of exactly ``size`` and not blank."""
    try:
        probe = probe_image_bytes(data)
        if probe.media_type != "image/png":
            raise MediaProbeError(f"the plate is {probe.media_type}, not a PNG")
        if (probe.width, probe.height) != size:
            raise MediaProbeError(
                f"the plate is {probe.width}x{probe.height}, expected "
                f"{size[0]}x{size[1]}"
            )
        if png_is_blank(data):
            raise MediaProbeError("the plate is blank")
    except MediaProbeError as exc:
        raise AnimationError(str(exc)) from exc


def is_out_of_memory(error: BaseException) -> bool:
    """True when a ComfyUI failure message says the GPU ran out of memory."""
    return _OOM_PATTERN.search(str(error)) is not None


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackgroundRecord:
    """What produced one plate (for the manifest)."""

    location_id: str
    method: str
    pass1_size: tuple[int, int]
    pass2_size: tuple[int, int]
    final_size: tuple[int, int]
    tiled_vae: bool
    post_resize: str | None
    seed: int
    prompt_id: str
    elapsed_s: float
    cache_hit: bool
    tier: str
    checkpoint: str
    path: str
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in asdict(self).items()
        }


def plate_method(plan: TwoPassPlan, tiled: bool) -> str:
    """Honest one-line description of how a plate was made."""
    text = (
        "ComfyUI SD1.5 two-pass hires-fix (txt2img "
        f"{plan.pass1_size[0]}x{plan.pass1_size[1]} -> img2img "
        f"{plan.pass2_size[0]}x{plan.pass2_size[1]}, denoise {plan.pass2_denoise:g})"
    )
    if tiled:
        text += ", tiled VAE"
    if plan.post_resize is not None:
        text += (
            f", then {plan.post_resize} to {plan.final_size[0]}x{plan.final_size[1]}"
        )
    return text


def plate_cache_key(
    location: LocationSpec, storyboard: Storyboard, checkpoint: str, plan: TwoPassPlan
) -> str:
    style = storyboard.style
    material = json.dumps(
        [
            CACHE_VERSION,
            location.prompt,
            style.background_style_suffix,
            style.negative_prompt,
            location.seed,
            checkpoint,
            plan.pass1_size,
            plan.pass1_steps,
            plan.pass1_cfg,
            plan.pass2_steps,
            plan.pass2_cfg,
            plan.pass2_denoise,
            plan.sampler,
            plan.scheduler,
            plan.final_size,
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_cached(
    cache_dir: Path, key: str, plan: TwoPassPlan
) -> tuple[bytes, dict[str, Any]] | None:
    png_path, meta_path = cache_dir / f"{key}.png", cache_dir / f"{key}.json"
    if not (png_path.is_file() and meta_path.is_file()):
        return None
    try:
        data = png_path.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        validate_plate(data, plan.final_size)
        BackgroundTier(meta["tier"])
        str(meta["prompt_id"])
        int(meta["seed"])
        float(meta["elapsed_s"])
    except (OSError, ValueError, KeyError, TypeError, AnimationError):
        return None
    return data, meta


def _free(comfy: Any, free_vram: Callable[[], object] | None) -> None:
    action = free_vram or getattr(comfy, "free_memory", None)
    if action is not None:
        try:
            action()
        except Exception:  # best effort between attempts; never a reason to stop
            return


def generate_backgrounds(
    comfy: Any,
    storyboard: Storyboard,
    out_dir: Path,
    cache_dir: Path,
    *,
    base_plan: TwoPassPlan | None = None,
    free_vram: Callable[[], object] | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, BackgroundRecord]:
    """One plate per storyboard location, written to ``out_dir/<location>.png``.

    ``comfy`` is a ``ComfyUIAdapter`` (its ``job_timeout_seconds`` should be
    ``JOB_TIMEOUT_S``). A cached plate is reused when it still validates. On an
    out-of-memory error the job is retried on the next tier; any other error, or
    running out of tiers, raises ``AnimationError``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    plan = base_plan or TwoPassPlan()
    records: dict[str, BackgroundRecord] = {}
    for location in storyboard.locations:
        key = plate_cache_key(location, storyboard, comfy.default_model, plan)
        cached = _load_cached(cache_dir, key, plan)
        if cached is not None:
            data, meta = cached
            records[location.id] = _write_plate(
                out_dir, location, data, meta, comfy.default_model, plan, hit=True
            )
            log(f"         backgrounds: {location.id} from cache (tier {meta['tier']})")
            continue
        data, meta = _generate_plate(
            comfy, storyboard, location, plan, free_vram=free_vram, log=log
        )
        (cache_dir / f"{key}.png").write_bytes(data)
        (cache_dir / f"{key}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        records[location.id] = _write_plate(
            out_dir, location, data, meta, comfy.default_model, plan, hit=False
        )
    return records


def _write_plate(
    out_dir: Path,
    location: LocationSpec,
    data: bytes,
    meta: dict[str, Any],
    checkpoint: str,
    base_plan: TwoPassPlan,
    *,
    hit: bool,
) -> BackgroundRecord:
    path = out_dir / f"{location.id}.png"
    path.write_bytes(data)
    tier = BackgroundTier(meta["tier"])
    plan = plan_for_tier(tier, base_plan)
    return BackgroundRecord(
        location_id=location.id,
        method=str(meta.get("method") or plate_method(plan, tier_uses_tiled_vae(tier))),
        pass1_size=plan.pass1_size,
        pass2_size=plan.pass2_size,
        final_size=plan.final_size,
        tiled_vae=tier_uses_tiled_vae(tier),
        post_resize=plan.post_resize,
        seed=int(meta["seed"]),
        prompt_id=str(meta["prompt_id"]),
        elapsed_s=float(meta["elapsed_s"]),
        cache_hit=hit,
        tier=tier.value,
        checkpoint=checkpoint,
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _generate_plate(
    comfy: Any,
    storyboard: Storyboard,
    location: LocationSpec,
    base_plan: TwoPassPlan,
    *,
    free_vram: Callable[[], object] | None,
    log: Callable[[str], None],
) -> tuple[bytes, dict[str, Any]]:
    style = storyboard.style
    positive = f"{location.prompt}, {style.background_style_suffix}"
    request = ImageGenerationRequest(
        scene_id="animation",
        shot_id=f"bg_{location.id}",
        prompt=location.prompt,
        width=base_plan.final_size[0],
        height=base_plan.final_size[1],
        style="animation_two_pass",
        seed_key=str(location.seed),
    )
    failures: list[str] = []
    for tier in TIER_ORDER:
        plan = plan_for_tier(tier, base_plan)
        tiled = tier_uses_tiled_vae(tier)

        def builder(
            _request: ImageGenerationRequest,
            seed: int,
            plan: TwoPassPlan = plan,
            tiled: bool = tiled,
        ) -> dict[str, Any]:
            return build_two_pass_graph(
                plan,
                positive,
                style.negative_prompt,
                seed,
                comfy.default_model,
                tiled,
                filename_prefix=f"anim_{location.id}",
            )

        log(
            f"         backgrounds: {location.id} tier {tier.value} "
            f"({plate_method(plan, tiled)})"
        )
        try:
            image, job = comfy.generate_with_job(request, graph_builder=builder)
        except ImageGenerationError as exc:
            if not is_out_of_memory(exc):
                raise AnimationError(
                    f"ComfyUI failed for background '{location.id}' on tier "
                    f"{tier.value}: {exc}"
                ) from exc
            failures.append(f"{tier.value}: {exc}")
            log(
                f"         backgrounds: {location.id} tier {tier.value} "
                "ran out of memory"
            )
            _free(comfy, free_vram)
            continue
        validate_plate(image.data, plan.final_size)
        return image.data, {
            "tier": tier.value,
            "method": plate_method(plan, tiled),
            "seed": job.seed,
            "prompt_id": job.prompt_id,
            "elapsed_s": job.elapsed_seconds,
        }
    raise AnimationError(
        f"background '{location.id}' ran out of GPU memory on every tier "
        f"({'; '.join(failures)}); no placeholder was made"
    )
