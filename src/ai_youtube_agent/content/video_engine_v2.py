"""Realistic Video Engine V2: Core Models, Presets, and Prompt Architecture.

Provides domain structures, character profiles, shot framing presets, and prompt
synthesis rules for the V2 AI Motion Video Pipeline.

Core Principles:
1. True AI-generated latent motion via verified baseline (SD 1.5 + AnimateDiff v2).
2. Character consistency via front-loaded CharacterProfile and controlled seed offsets.
3. Strict enforcement of SD 1.5 CLIP 77-token ceiling (front-loading subject and action,
   avoiding boilerplate metadata headers).
4. Shot framing presets (Close-up Portrait, Medium, Wide, Establishing, Insert Detail).
5. Clear separation between AI latent motion and simulated post-processing camera moves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

# Standard CLIP 77-token ceiling buffer (target under 75 tokens for safety)
MAX_CLIP_TOKENS: Final[int] = 75
ROUGH_WORDS_PER_TOKEN: Final[float] = 0.75  # ~1.3 tokens per word on average


class V2ShotPreset(StrEnum):
    """Cinematic camera framing presets for AI motion clip generation."""

    CLOSE_UP_PORTRAIT = "close_up_portrait"
    MEDIUM_SHOT = "medium_shot"
    WIDE_SHOT = "wide_shot"
    ESTABLISHING_SHOT = "establishing_shot"
    INSERT_DETAIL = "insert_detail"

    @property
    def framing_prompt_prefix(self) -> str:
        """Visual framing prompt tokens anchored to prevent ambiguous latent composition.
        
        Specifically avoids 'extreme close-up' or 'macro' for human subjects to prevent
        accidental skin-pore and eye magnification.
        """
        match self:
            case V2ShotPreset.CLOSE_UP_PORTRAIT:
                return "head and shoulders portrait of, chest up, natural facial framing, centered,"
            case V2ShotPreset.MEDIUM_SHOT:
                return "medium waist-up shot of, natural posture, centered,"
            case V2ShotPreset.WIDE_SHOT:
                return "full-body wide shot, head to toe, full length, feet on ground, upright posture,"
            case V2ShotPreset.ESTABLISHING_SHOT:
                return "cinematic establishing wide shot of, atmospheric,"
            case V2ShotPreset.INSERT_DETAIL:
                return "cinematic medium insert shot focusing on,"


class V2AspectRatioPreset(StrEnum):
    """Supported aspect ratios for latent generation and final video output."""

    VERTICAL_9_16 = "9:16"
    HORIZONTAL_16_9 = "16:9"
    SQUARE_1_1 = "1:1"

    @property
    def latent_dimensions(self) -> tuple[int, int]:
        """Optimal SD 1.5 latent resolution conforming to 6 GB VRAM budget (<= 262,144 pixels)."""
        match self:
            case V2AspectRatioPreset.VERTICAL_9_16:
                return (384, 640)  # 245,760 pixels (< 512x512 = 262,144)
            case V2AspectRatioPreset.HORIZONTAL_16_9:
                return (640, 384)  # 245,760 pixels
            case V2AspectRatioPreset.SQUARE_1_1:
                return (512, 512)  # 262,144 pixels

    @property
    def target_canvas(self) -> tuple[int, int]:
        """Standard Full-HD canvas dimensions."""
        match self:
            case V2AspectRatioPreset.VERTICAL_9_16:
                return (1080, 1920)
            case V2AspectRatioPreset.HORIZONTAL_16_9:
                return (1920, 1080)
            case V2AspectRatioPreset.SQUARE_1_1:
                return (1080, 1080)


class V2MotionIntensity(StrEnum):
    """Degrees of physical and environmental motion requested in the clip."""

    SUBTLE = "subtle"          # Micro-motion: eye blinks, gentle breathing, soft hair drift
    MODERATE = "moderate"      # Natural gesture: holding tea, turning head, hand movement
    DYNAMIC = "dynamic"        # Locomotion: walking, flowing garments, environmental wind


@dataclass(frozen=True)
class V2CharacterProfile:
    """Rich visual character profile maintaining visual consistency across scenes."""

    character_id: str
    name: str
    demographics: str
    facial_features: str
    hair: str
    wardrobe: str
    distinguishing_features: str = ""
    default_negative: str = (
        "extreme close-up, macro skin, skin pores, eye macro, cropped, cut off, out of frame, "
        "leaning back, backward bend, arching spine, contorted body, distorted neck, falling backwards, "
        "sliding feet, ice skating, hovering feet, floating, flying, deformed face, bad eyes, disfigured, "
        "bad teeth, bad anatomy, cross-eyed, mutated hands, extra limbs, extra fingers, blurry, "
        "low quality, worst quality, distorted, static, ugly, cartoon, 3d render, doll, plastic skin"
    )

    def core_identity_tokens(self) -> str:
        """Compact string representing the character's unvarying identity."""
        parts = [self.demographics, self.name, self.facial_features, self.hair]
        if self.distinguishing_features:
            parts.append(self.distinguishing_features)
        return ", ".join(p.strip() for p in parts if p.strip())

    def build_prompt(
        self,
        *,
        preset: V2ShotPreset,
        action: str,
        setting: str = "",
        lighting_and_style: str = "soft warm morning sunlight, cinematic lighting, photorealistic, 8k, masterpiece",
        aspect_ratio: V2AspectRatioPreset = V2AspectRatioPreset.SQUARE_1_1,
    ) -> str:
        """Synthesizes a front-loaded prompt conforming to the CLIP token budget.
        
        Order of priority:
        1. Framing prefix (anchors composition so face or full-body is positioned correctly).
        2. Core character identity (name, demographics, face, hair).
        3. Wardrobe tokens (keeps clothing consistent).
        4. Single primary action (clear focused movement).
        5. Setting (environmental context).
        6. Lighting and quality tokens.
        """
        framing = preset.framing_prompt_prefix
        identity = self.core_identity_tokens()
        wardrobe = self.wardrobe.strip()
        action_clean = action.strip()
        setting_clean = setting.strip()

        components: list[str] = [framing, identity]
        if wardrobe:
            components.append(wardrobe)
        if action_clean:
            components.append(action_clean)
        if setting_clean:
            components.append(setting_clean)
        if lighting_and_style:
            components.append(lighting_and_style.strip())

        full_prompt = ", ".join(c for c in components if c)
        return full_prompt

    def as_dict(self) -> dict[str, Any]:
        return {
            "character_id": self.character_id,
            "name": self.name,
            "demographics": self.demographics,
            "facial_features": self.facial_features,
            "hair": self.hair,
            "wardrobe": self.wardrobe,
            "distinguishing_features": self.distinguishing_features,
        }


@dataclass(frozen=True)
class V2SceneStorySpec:
    """Rich cinematic storyboard specification for an individual story scene."""

    scene_id: str
    order_index: int
    title: str
    narrative_purpose: str
    character_profile: V2CharacterProfile | None
    location_name: str
    time_and_lighting: str
    shot_type: V2ShotPreset
    primary_action: str
    camera_motion: str
    opening_state: str
    closing_state: str
    transition_to_next: str
    target_duration_s: float
    seed: int

    def build_prompt(
        self,
        aspect_ratio: V2AspectRatioPreset = V2AspectRatioPreset.VERTICAL_9_16,
    ) -> str:
        """Constructs an exact, front-loaded prompt for this scene's shot type and narrative."""
        if self.character_profile:
            return self.character_profile.build_prompt(
                preset=self.shot_type,
                action=self.primary_action,
                setting=self.location_name,
                lighting_and_style=self.time_and_lighting,
                aspect_ratio=aspect_ratio,
            )
        # Non-character shot (e.g. establishing street shot or prop insert)
        framing = self.shot_type.framing_prompt_prefix
        components = [framing, self.primary_action, self.location_name, self.time_and_lighting]
        return ", ".join(c.strip() for c in components if c.strip())

    def build_negative_prompt(self) -> str:
        """Returns targeted negative tokens tailored to the shot type."""
        base_neg = (
            "deformed face, bad eyes, disfigured, bad teeth, bad anatomy, cross-eyed, mutated hands, "
            "extra limbs, extra fingers, blurry, low quality, worst quality, distorted, static, ugly, "
            "cartoon, 3d render, doll, plastic skin"
        )
        if self.shot_type in (V2ShotPreset.CLOSE_UP_PORTRAIT, V2ShotPreset.MEDIUM_SHOT):
            return f"extreme close-up, macro skin, skin pores, eye macro, cropped, out of frame, {base_neg}"
        elif self.shot_type == V2ShotPreset.WIDE_SHOT:
            return (
                f"cropped, cut off, headshot, close up, medium shot, out of frame, "
                f"leaning back, backward bend, arching spine, contorted body, falling backwards, "
                f"floating feet, sliding feet, ice skating, balloon skirt, {base_neg}"
            )
        elif self.shot_type == V2ShotPreset.INSERT_DETAIL:
            return f"human face, body parts, deformed hands, distorted ceramic, broken spoon, blurry, ugly, {base_neg}"
        return base_neg


@dataclass(frozen=True)
class FramingValidationResult:
    """Outcome of automated framing and composition inspection."""

    is_valid: bool
    shot_type: V2ShotPreset
    has_extreme_macro: bool
    has_edge_clipping: bool
    details: str


def validate_framing_and_composition(
    image_width: int,
    image_height: int,
    expected_shot: V2ShotPreset,
    sample_pixels: list[list[int]] | None = None,
) -> FramingValidationResult:
    """Automated inspection to catch abnormal micro-crops or extreme zooms."""
    # Ratio sanity
    aspect = image_width / float(image_height) if image_height > 0 else 1.0
    has_extreme_macro = False
    has_edge_clipping = False
    issues = []

    # If pixel data is available (e.g. RGB distribution), check for solid-color/micro-texture dominance
    if sample_pixels:
        # Check variance across sampled patches
        variances = []
        for patch in sample_pixels:
            if patch:
                mean_p = sum(patch) / len(patch)
                var_p = sum((p - mean_p) ** 2 for p in patch) / len(patch)
                variances.append(var_p)
        avg_var = sum(variances) / len(variances) if variances else 0.0
        # Exceptionally low variance with high skin-tone luminance indicates skin-pore zoom
        if avg_var < 15.0 and expected_shot == V2ShotPreset.CLOSE_UP_PORTRAIT:
            has_extreme_macro = True
            issues.append("Low texture variance indicates skin-pore micro zoom")

    is_valid = not (has_extreme_macro or has_edge_clipping)
    detail_str = "; ".join(issues) if issues else "Framing conforms to cinematic preset"
    return FramingValidationResult(
        is_valid=is_valid,
        shot_type=expected_shot,
        has_extreme_macro=has_extreme_macro,
        has_edge_clipping=has_edge_clipping,
        details=detail_str,
    )


@dataclass(frozen=True)
class V2ClipGenerationConfig:
    """Canonical sampling parameters for AnimateDiff SD 1.5 motion generation."""

    base_checkpoint: str = "v1-5-pruned-emaonly.safetensors"
    motion_module: str = "mm_sd_v15_v2.ckpt"
    sampler_name: str = "dpmpp_2m"
    scheduler: str = "karras"
    steps: int = 16
    cfg: float = 6.0
    fps: float = 8.0
    frame_count: int = 16
    latent_width: int = 512
    latent_height: int = 512
    denoise: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_checkpoint": self.base_checkpoint,
            "motion_module": self.motion_module,
            "sampler_name": self.sampler_name,
            "scheduler": self.scheduler,
            "steps": self.steps,
            "cfg": self.cfg,
            "fps": self.fps,
            "frame_count": self.frame_count,
            "latent_width": self.latent_width,
            "latent_height": self.latent_height,
            "denoise": self.denoise,
        }


@dataclass(frozen=True)
class V2QualityEnhancementSurvey:
    """Documented survey results and hardware feasibility for optional enhancement tools."""

    rife_available: bool = False
    rife_version: str = "4.22.lite (Practical-RIFE / ECCV 2022)"
    rife_license: str = "MIT License (Megvii Inc.)"
    rife_vram_requirement_mb: int = 1200
    rife_target_fps: int = 24
    rife_feasibility_note: str = (
        "RIFE 4.22.lite is well-suited for 8 FPS to 24 FPS interpolation on GTX 1660 SUPER (6 GB). "
        "Must be executed sequentially after ComfyUI frees VRAM to prevent Out-Of-Memory spikes."
    )

    realesrgan_available: bool = False
    realesrgan_version: str = "Real-ESRGAN (realesrgan-ncnn-vulkan / PyTorch)"
    realesrgan_license: str = "BSD 3-Clause"
    realesrgan_risk_note: str = (
        "2D frame-by-frame super-resolution introduces temporal boiling noise / texture flickering "
        "unless temporal consistency regularization is applied. Downstream FFmpeg Lanczos / bicubic "
        "canvas scaling is significantly more temporally stable for SD 1.5 video."
    )

    vhs_note: str = (
        "ComfyUI-VideoHelperSuite is redundant because native ComfyUI SaveWEBM + system FFmpeg "
        "provides robust, zero-dependency encoding, probing, and cross-transcoding."
    )
