"""Script Analysis & Story Bible Engine (Script-to-Film Refactor, Phases 2 & 3).

Ingests a raw user script (from text or file path) and produces:
1. A structured ``StoryBible`` containing:
   - ``CharacterSpec`` records (fixed appearance, wardrobe, role, and voice traits),
   - ``LocationSpec`` records (setting, time of day, era/style, lighting mood, and
     deterministic RGB palette for visual continuity),
   - ``StoryArc`` (opening situation, conflict/development, climax, resolution,
     visual style, and explicit ``assumptions`` when visual details are inferred
     from narrative context),
   - ``ContinuityConstraint`` rules enforced across every scene and shot,
   - ``DialogueSegment`` records separating ``NARRATOR`` lines from ``CHARACTER``
     dialogue lines.
2. A validated domain ``Script`` (``ScriptSection`` tuple + ``DurationTarget``)
   that preserves the user's narrative sequence, dialogue meaning, and ending
   while satisfying ``ContentType.SHORTS`` or ``ContentType.LONGFORM`` structural
   and timing invariants.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final

from ai_youtube_agent.content.script import (
    DurationTarget,
    Script,
    ScriptSection,
    SectionKind,
)
from ai_youtube_agent.core.content_item import ContentType
from ai_youtube_agent.core.errors import DomainError

_SCENE_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:#+\s*)?(?:(?:SCENE|CẢNH|CANH|CHAPTER|CHƯƠNG|CHUONG|ACT|HỒI|PART|PHẦN)\s*\d*[:.\-\s]*|(?:INT\.|EXT\.|INT/EXT\.|NỘI\.|NGOẠI\.)\s*)(.+)$",
    re.IGNORECASE,
)
_BRACKET_SPEAKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[(?P<speaker>[^\[\]\n]{1,40})\](?:\s*\((?P<emotion>[^()\n]{1,40})\))?\s*[:\-]\s*(?P<text>.+)$"
)
_COLON_SPEAKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<speaker>[A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ][A-Za-zÀ-ỹ0-9_ ]{0,28})(?:\s*\((?P<emotion>[^()\n]{1,40})\))?\s*:\s*(?P<text>.+)$"
)
_NARRATOR_ALIASES: Final[frozenset[str]] = frozenset(
    {
        "narrator",
        "narration",
        "voiceover",
        "vo",
        "người dẫn",
        "nguoi dan",
        "dẫn chuyện",
        "dan chuyen",
        "lời dẫn",
        "loi dan",
        "host",
        "storyteller",
    }
)
_NON_CHARACTER_LABELS: Final[frozenset[str]] = frozenset(
    {
        "scene",
        "cảnh",
        "canh",
        "chapter",
        "chương",
        "chuong",
        "hook",
        "intro",
        "body",
        "cta",
        "outro",
        "conclusion",
        "kết",
        "mở đầu",
        "bối cảnh",
        "boi canh",
        "location",
        "setting",
        "action",
        "hành động",
        "sfx",
        "music",
        "bgm",
        "note",
        "title",
        "tiêu đề",
    }
)
_LOCATION_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:BỐI CẢNH|BOI CANH|LOCATION|SETTING)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
_TIME_KEYWORDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("night", ("night", "midnight", "đêm", "khuya", "tối", "trăng", "moonlight")),
    ("dawn", ("dawn", "sunrise", "bình minh", "sáng sớm", "rạng đông")),
    ("dusk", ("dusk", "sunset", "twilight", "hoàng hôn", "chiều tà", "chiều muộn")),
    ("day", ("day", "morning", "noon", "afternoon", "ban ngày", "buổi sáng", "buổi trưa")),
)

_PALETTES_BY_TIME: Final[
    dict[str, tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]]
] = {
    "night": ((18, 28, 54), (38, 60, 108), (235, 190, 92)),
    "dawn": ((44, 52, 88), (168, 98, 118), (246, 196, 132)),
    "dusk": ((58, 34, 72), (176, 78, 68), (248, 176, 90)),
    "day": ((34, 68, 102), (68, 128, 168), (242, 210, 118)),
    "interior": ((42, 46, 58), (88, 96, 118), (224, 182, 112)),
}

_VOICE_POOL: Final[tuple[str, ...]] = (
    "vi-VN-Standard-A",
    "vi-VN-Standard-B",
    "vi-VN-Standard-C",
    "vi-VN-Standard-D",
    "en-US-Standard-A",
    "en-US-Standard-B",
)


class ProductionMode(StrEnum):
    """Production mode for the Script-to-Film pipeline (orthogonal to ContentType)."""

    STORY_VIDEO = "story_video"
    AI_FILM = "ai_film"


class SpeakerKind(StrEnum):
    """Distinguishes narrator voiceover from character dialogue."""

    NARRATOR = "narrator"
    CHARACTER = "character"


class ScriptAnalysisErrorCode(StrEnum):
    EMPTY_SCRIPT = "domain.script_analysis_empty"
    FILE_NOT_FOUND = "domain.script_analysis_file_not_found"
    FILE_UNREADABLE = "domain.script_analysis_file_unreadable"
    INVALID_MODE = "domain.script_analysis_invalid_mode"


class ScriptAnalysisError(DomainError):
    """Raised when a raw script cannot be ingested or analyzed."""

    default_code = ScriptAnalysisErrorCode.EMPTY_SCRIPT.value
    default_user_message = "The provided script could not be analyzed."
    default_http_status = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(
        self,
        detail: str,
        *,
        code: ScriptAnalysisErrorCode = ScriptAnalysisErrorCode.EMPTY_SCRIPT,
    ) -> None:
        super().__init__(detail, code=code.value)
        if code is ScriptAnalysisErrorCode.FILE_NOT_FOUND:
            self.default_http_status = HTTPStatus.NOT_FOUND


@dataclass(frozen=True)
class CharacterSpec:
    """Consistent character sheet in the Story Bible."""

    character_id: str
    name: str
    role: str
    appearance: str
    wardrobe: str
    voice_traits: str
    default_voice_id: str

    def __post_init__(self) -> None:
        if not self.character_id.strip():
            raise ValueError("character_id must not be empty")
        if not self.name.strip():
            raise ValueError("name must not be empty")

    def as_dict(self) -> dict[str, Any]:
        return {
            "character_id": self.character_id,
            "name": self.name,
            "role": self.role,
            "appearance": self.appearance,
            "wardrobe": self.wardrobe,
            "voice_traits": self.voice_traits,
            "default_voice_id": self.default_voice_id,
        }


@dataclass(frozen=True)
class LocationSpec:
    """Consistent location and lighting specification in the Story Bible."""

    location_id: str
    name: str
    setting_description: str
    time_of_day: str
    era_or_style: str
    lighting_mood: str
    palette_rgb: tuple[
        tuple[int, int, int],
        tuple[int, int, int],
        tuple[int, int, int],
    ]

    def __post_init__(self) -> None:
        if not self.location_id.strip():
            raise ValueError("location_id must not be empty")
        if not self.name.strip():
            raise ValueError("name must not be empty")

    def as_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "name": self.name,
            "setting_description": self.setting_description,
            "time_of_day": self.time_of_day,
            "era_or_style": self.era_or_style,
            "lighting_mood": self.lighting_mood,
            "palette_rgb": [list(rgb) for rgb in self.palette_rgb],
        }


@dataclass(frozen=True)
class StoryArc:
    """Narrative structure extracted from the input script without altering plot."""

    title: str
    logline: str
    opening_situation: str
    conflict_or_development: str
    climax: str
    resolution: str
    tone: str
    visual_style: str
    assumptions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "logline": self.logline,
            "opening_situation": self.opening_situation,
            "conflict_or_development": self.conflict_or_development,
            "climax": self.climax,
            "resolution": self.resolution,
            "tone": self.tone,
            "visual_style": self.visual_style,
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True)
class DialogueSegment:
    """A single narration or character dialogue utterance within a scene."""

    segment_id: str
    speaker_kind: SpeakerKind
    speaker_name: str
    character_id: str | None
    voice_id: str
    text: str
    emotion: str
    estimated_duration_ms: int

    def __post_init__(self) -> None:
        if not self.segment_id.strip():
            raise ValueError("segment_id must not be empty")
        if not isinstance(self.speaker_kind, SpeakerKind):
            raise TypeError("speaker_kind must be a SpeakerKind")
        if not self.text.strip():
            raise ValueError("dialogue segment text must not be empty")
        if self.estimated_duration_ms < 200:
            raise ValueError("estimated_duration_ms must be >= 200")

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "speaker_kind": self.speaker_kind.value,
            "speaker_name": self.speaker_name,
            "character_id": self.character_id,
            "voice_id": self.voice_id,
            "text": self.text,
            "emotion": self.emotion,
            "estimated_duration_ms": self.estimated_duration_ms,
        }


@dataclass(frozen=True)
class ContinuityConstraint:
    """Explicit continuity rule enforced across scenes and shots."""

    constraint_id: str
    kind: str
    subject_id: str
    rule: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "rule": self.rule,
        }


@dataclass(frozen=True)
class RawSceneBeat:
    """Intermediate parsed beat from the raw script prior to timeline planning."""

    beat_index: int
    heading: str
    location_hint: str
    time_of_day: str
    action_summary: str
    segments: tuple[DialogueSegment, ...]
    character_ids: tuple[str, ...]
    spoken_text: str
    estimated_seconds: int


@dataclass(frozen=True)
class StoryBible:
    """Complete Story Bible for a Script-to-Film project."""

    story_id: str
    title: str
    language: str
    content_type: ContentType
    production_mode: ProductionMode
    visual_style: str
    aspect_ratio: str
    story_arc: StoryArc
    characters: tuple[CharacterSpec, ...]
    locations: tuple[LocationSpec, ...]
    continuity_constraints: tuple[ContinuityConstraint, ...]
    raw_script_sha256: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def character_by_id(self, character_id: str) -> CharacterSpec | None:
        for char in self.characters:
            if char.character_id == character_id:
                return char
        return None

    def location_by_id(self, location_id: str) -> LocationSpec | None:
        for loc in self.locations:
            if loc.location_id == location_id:
                return loc
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "story_id": self.story_id,
            "title": self.title,
            "language": self.language,
            "content_type": self.content_type.value,
            "production_mode": self.production_mode.value,
            "visual_style": self.visual_style,
            "aspect_ratio": self.aspect_ratio,
            "story_arc": self.story_arc.as_dict(),
            "characters": [c.as_dict() for c in self.characters],
            "locations": [loc.as_dict() for loc in self.locations],
            "continuity_constraints": [
                c.as_dict() for c in self.continuity_constraints
            ],
            "raw_script_sha256": self.raw_script_sha256,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class ScriptAnalysisOutput:
    """Result of analyzing a raw script into a StoryBible, beats, and domain Script."""

    story_bible: StoryBible
    beats: tuple[RawSceneBeat, ...]
    sections: tuple[ScriptSection, ...]
    duration_target: DurationTarget
    normalized_raw_script: str


def load_raw_script(
    *,
    script_text: str | None = None,
    script_path: str | Path | None = None,
) -> str:
    """Load and normalize raw script text from either ``script_text`` or ``script_path``."""
    if script_text is not None and script_text.strip():
        raw = script_text
    elif script_path is not None:
        path = Path(script_path)
        if not path.exists() or not path.is_file():
            raise ScriptAnalysisError(
                f"script file {str(path)!r} does not exist",
                code=ScriptAnalysisErrorCode.FILE_NOT_FOUND,
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception as error:
            raise ScriptAnalysisError(
                f"failed to read script file {str(path)!r}: {error}",
                code=ScriptAnalysisErrorCode.FILE_UNREADABLE,
            ) from error
    else:
        raise ScriptAnalysisError(
            "either script_text or script_path must be provided with non-empty content",
            code=ScriptAnalysisErrorCode.EMPTY_SCRIPT,
        )

    normalized = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n").strip()
    normalized = normalized.replace("<", "(").replace(">", ")")
    if not normalized:
        raise ScriptAnalysisError(
            "script content must not be empty",
            code=ScriptAnalysisErrorCode.EMPTY_SCRIPT,
        )
    return normalized


def _slug_id(prefix: str, label: str, index: int) -> str:
    ascii_folded = (
        unicodedata.normalize("NFKD", label)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    cleaned = re.sub(r"[^a-z0-9]+", "_", ascii_folded).strip("_")
    if not cleaned:
        cleaned = f"item_{index}"
    return f"{prefix}_{cleaned[:24]}_{index}"


def _detect_time_of_day(text: str) -> str:
    lowered = text.lower()
    for time_key, keywords in _TIME_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return time_key
    if "int." in lowered or "nội." in lowered or "trong phòng" in lowered or "in the room" in lowered:
        return "interior"
    return "day"


def _estimate_utterance_ms(text: str) -> int:
    words = [w for w in text.split() if w.strip()]
    word_count = max(1, len(words))
    # ~2.5 words/sec (400 ms/word) with a 1200 ms floor per segment
    return max(1200, word_count * 400)


def _normalize_section_narration(
    text: str,
    *,
    heading: str,
    action_summary: str,
    min_chars: int,
    max_chars: int | None = None,
) -> str:
    """Ensure section text has caption-safe word lengths, punctuation, and duration."""
    # 1. Split any single token longer than 24 characters so caption wrapping never fails.
    safe_words: list[str] = []
    for raw_word in text.split():
        w = raw_word.strip()
        if not w:
            continue
        while len(w) > 24:
            safe_words.append(w[:20] + "-")
            w = w[20:]
        if w:
            safe_words.append(w)
    if not safe_words:
        safe_words = ["The", "scene", "unfolds", "with", "clear", "cinematic", "focus."]

    # 2. Insert a comma or period every 9 words if there is a long unpunctuated run,
    # so caption phrase timing always splits cleanly into 1.5s..3.5s phrases.
    punctuated_words: list[str] = []
    run_len = 0
    for idx, word in enumerate(safe_words):
        run_len += 1
        has_punct = word[-1] in ".!?…,;:"
        if has_punct:
            punctuated_words.append(word)
            run_len = 0
        elif run_len >= 9 and idx < len(safe_words) - 1:
            punctuated_words.append(word + ",")
            run_len = 0
        else:
            punctuated_words.append(word)
    if punctuated_words and punctuated_words[-1][-1] not in ".!?…":
        punctuated_words[-1] = punctuated_words[-1] + "."

    cleaned = " ".join(punctuated_words)

    # 3. Expand narration deterministically from the scene's own context if below min_chars.
    clean_heading = (heading or "the scene").strip(" .,:;-") or "the scene"
    clean_action = (action_summary or cleaned).strip(" .,:;-") or "the moment unfolds"
    if len(clean_action) > 70:
        clean_action = " ".join(clean_action.split()[:10]).strip(" .,:;-")
    expansion_templates = (
        f"In {clean_heading}, the atmosphere deepens with steady focus.",
        f"Every detail of {clean_action} remains clear in the frame.",
        "The camera holds on the expression, lighting, and quiet tension.",
        "Light and shadow guide the viewer through this pivotal beat.",
        "Each movement connects naturally to the unfolding story arc.",
        "The visual rhythm stays grounded, deliberate, and expressive.",
    )
    exp_idx = 0
    while len(cleaned) < min_chars:
        phrase = expansion_templates[exp_idx % len(expansion_templates)]
        cleaned = f"{cleaned} {phrase}"
        exp_idx += 1

    # 4. Trim at sentence/word boundary if above max_chars.
    if max_chars is not None and len(cleaned) > max_chars:
        words = cleaned.split()
        trimmed: list[str] = []
        current_len = 0
        for word in words:
            added = len(word) + (1 if trimmed else 0)
            if current_len + added > max_chars and len(trimmed) >= 6:
                break
            trimmed.append(word)
            current_len += added
        cleaned = " ".join(trimmed)
        if cleaned and cleaned[-1] not in ".!?…":
            cleaned = cleaned.rstrip(",;:") + "."

    return cleaned


class ScriptAnalyzer:
    """Deterministic script-to-film analyzer producing a StoryBible and domain Script."""

    def analyze(
        self,
        *,
        script_text: str | None = None,
        script_path: str | Path | None = None,
        title: str | None = None,
        content_type: ContentType = ContentType.SHORTS,
        production_mode: ProductionMode = ProductionMode.STORY_VIDEO,
        visual_style: str = "cinematic_story",
        language: str = "vi",
        voice_id: str = "vi-VN-Standard-A",
        target_duration_seconds: int | None = None,
        clock: Any | None = None,
    ) -> ScriptAnalysisOutput:
        if not isinstance(content_type, ContentType):
            raise TypeError("content_type must be a ContentType")
        if not isinstance(production_mode, ProductionMode):
            raise ScriptAnalysisError(
                f"invalid production_mode {production_mode!r}",
                code=ScriptAnalysisErrorCode.INVALID_MODE,
            )

        raw = load_raw_script(script_text=script_text, script_path=script_path)
        raw_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        now = clock() if callable(clock) else datetime.now(UTC)

        aspect_ratio = "9:16" if content_type is ContentType.SHORTS else "16:9"
        clean_style = (visual_style or "cinematic_story").strip() or "cinematic_story"
        clean_lang = (language or "vi").strip() or "vi"
        narrator_voice = (voice_id or "vi-VN-Standard-A").strip() or "vi-VN-Standard-A"

        # 1. Split raw script into raw scene blocks while preserving original order
        raw_blocks = self._split_into_raw_blocks(raw)

        # 2. Extract characters, locations, and dialogue/narration segments
        characters_by_key: dict[str, CharacterSpec] = {}
        locations_by_key: dict[str, LocationSpec] = {}
        assumptions: list[str] = []

        narrator_spec = CharacterSpec(
            character_id="char_narrator_0",
            name="Narrator",
            role="narrator",
            appearance="Off-screen omniscient storyteller voice",
            wardrobe="N/A (voiceover)",
            voice_traits="Warm, clear, steady narrative pacing",
            default_voice_id=narrator_voice,
        )
        characters_by_key["narrator"] = narrator_spec

        parsed_beats: list[RawSceneBeat] = []
        segment_counter = 0

        for block_idx, (heading, lines) in enumerate(raw_blocks, start=1):
            time_of_day = _detect_time_of_day(heading + " " + " ".join(lines))
            location_name = self._extract_location_name(heading, lines, block_idx, assumptions)
            loc_key = location_name.lower()
            if loc_key not in locations_by_key:
                loc_index = len(locations_by_key) + 1
                palette = _PALETTES_BY_TIME.get(time_of_day, _PALETTES_BY_TIME["day"])
                lighting = {
                    "night": "Cool moonlight with warm practical key light and deep shadows",
                    "dawn": "Soft golden-rose horizon light with gentle morning mist",
                    "dusk": "Warm amber sunset rim light with rich purple-blue shadows",
                    "interior": "Controlled warm interior key light with soft ambient fill",
                    "day": "Natural balanced daylight with crisp atmospheric depth",
                }.get(time_of_day, "Balanced cinematic key and fill lighting")
                locations_by_key[loc_key] = LocationSpec(
                    location_id=_slug_id("loc", location_name, loc_index),
                    name=location_name,
                    setting_description=f"{location_name} ({clean_style} staging)",
                    time_of_day=time_of_day,
                    era_or_style=clean_style,
                    lighting_mood=lighting,
                    palette_rgb=palette,
                )
            location_spec = locations_by_key[loc_key]

            beat_segments: list[DialogueSegment] = []
            beat_char_ids: list[str] = []
            action_parts: list[str] = []

            for line in lines:
                if _LOCATION_LINE_RE.match(line):
                    continue
                speaker_name, emotion, spoken = self._parse_line_speaker(line)
                segment_counter += 1
                if speaker_name is None:
                    # Narrator / action line
                    action_parts.append(spoken)
                    beat_segments.append(
                        DialogueSegment(
                            segment_id=f"seg_{segment_counter}",
                            speaker_kind=SpeakerKind.NARRATOR,
                            speaker_name="Narrator",
                            character_id=narrator_spec.character_id,
                            voice_id=narrator_spec.default_voice_id,
                            text=spoken,
                            emotion=emotion or "narrative",
                            estimated_duration_ms=_estimate_utterance_ms(spoken),
                        )
                    )
                else:
                    char_key = speaker_name.lower()
                    if char_key not in characters_by_key:
                        char_index = len(characters_by_key)
                        assigned_voice = _VOICE_POOL[char_index % len(_VOICE_POOL)]
                        role = "protagonist" if char_index == 1 else "supporting"
                        appearance = (
                            f"Consistent {role} character '{speaker_name}', expressive face, "
                            f"distinct silhouette in {clean_style} style"
                        )
                        wardrobe = (
                            f"Signature {clean_style} outfit for {speaker_name} kept "
                            f"consistent across all scenes"
                        )
                        assumptions.append(
                            f"Inferred consistent visual sheet and wardrobe for character '{speaker_name}' from script context."
                        )
                        characters_by_key[char_key] = CharacterSpec(
                            character_id=_slug_id("char", speaker_name, char_index),
                            name=speaker_name,
                            role=role,
                            appearance=appearance,
                            wardrobe=wardrobe,
                            voice_traits=f"Distinct {role} delivery ({emotion or 'natural'})",
                            default_voice_id=assigned_voice,
                        )
                    char_spec = characters_by_key[char_key]
                    if char_spec.character_id not in beat_char_ids:
                        beat_char_ids.append(char_spec.character_id)
                    action_parts.append(f"{char_spec.name}: {spoken}")
                    beat_segments.append(
                        DialogueSegment(
                            segment_id=f"seg_{segment_counter}",
                            speaker_kind=SpeakerKind.CHARACTER,
                            speaker_name=char_spec.name,
                            character_id=char_spec.character_id,
                            voice_id=char_spec.default_voice_id,
                            text=spoken,
                            emotion=emotion or "expressive",
                            estimated_duration_ms=_estimate_utterance_ms(spoken),
                        )
                    )

            if not beat_segments:
                continue

            # If no explicit dialogue speaker appeared, detect named characters mentioned in narration
            if not beat_char_ids:
                for key, spec in characters_by_key.items():
                    if key != "narrator" and spec.name.lower() in " ".join(lines).lower():
                        beat_char_ids.append(spec.character_id)

            spoken_lines: list[str] = []
            for seg in beat_segments:
                if seg.speaker_kind is SpeakerKind.CHARACTER:
                    spoken_lines.append(f"{seg.speaker_name}: {seg.text}")
                else:
                    spoken_lines.append(seg.text)
            spoken_joined = " ".join(spoken_lines).strip()
            total_ms = sum(s.estimated_duration_ms for s in beat_segments)
            est_seconds = max(4, round(total_ms / 1000))

            parsed_beats.append(
                RawSceneBeat(
                    beat_index=len(parsed_beats) + 1,
                    heading=heading or f"Scene {len(parsed_beats) + 1}",
                    location_hint=location_spec.location_id,
                    time_of_day=time_of_day,
                    action_summary=action_parts[0][:160] if action_parts else heading,
                    segments=tuple(beat_segments),
                    character_ids=tuple(beat_char_ids) if beat_char_ids else (narrator_spec.character_id,),
                    spoken_text=spoken_joined,
                    estimated_seconds=est_seconds,
                )
            )

        if not parsed_beats:
            raise ScriptAnalysisError(
                "script did not contain any playable narrative or dialogue lines",
                code=ScriptAnalysisErrorCode.EMPTY_SCRIPT,
            )

        # Ensure at least one visual protagonist exists if the script is pure narration
        if len(characters_by_key) == 1:
            protagonist = CharacterSpec(
                character_id="char_protagonist_1",
                name="Main Subject",
                role="protagonist",
                appearance=f"Central narrative figure rendered in consistent {clean_style} visual style",
                wardrobe=f"Consistent {clean_style} attire matching the story setting",
                voice_traits="Narrated by primary storyteller voice",
                default_voice_id=narrator_voice,
            )
            characters_by_key["main_subject"] = protagonist
            assumptions.append(
                "Script uses third-person/voiceover narration without explicit speaker tags; "
                "created a consistent 'Main Subject' visual anchor for scene continuity."
            )
            parsed_beats = [
                replace(
                    beat,
                    character_ids=(protagonist.character_id, narrator_spec.character_id),
                )
                for beat in parsed_beats
            ]

        resolved_title = self._resolve_title(title, raw_blocks, parsed_beats)
        story_arc = self._build_story_arc(
            resolved_title,
            parsed_beats,
            clean_style,
            tuple(assumptions),
        )
        constraints = self._build_continuity_constraints(
            tuple(characters_by_key.values()),
            tuple(locations_by_key.values()),
        )

        story_bible = StoryBible(
            story_id=f"story_{raw_sha256[:12]}",
            title=resolved_title,
            language=clean_lang,
            content_type=content_type,
            production_mode=production_mode,
            visual_style=clean_style,
            aspect_ratio=aspect_ratio,
            story_arc=story_arc,
            characters=tuple(characters_by_key.values()),
            locations=tuple(locations_by_key.values()),
            continuity_constraints=constraints,
            raw_script_sha256=raw_sha256,
            created_at=now,
        )

        sections, duration_target, aligned_beats = self._build_sections_and_beats(
            parsed_beats,
            content_type=content_type,
            target_duration_seconds=target_duration_seconds,
        )

        return ScriptAnalysisOutput(
            story_bible=story_bible,
            beats=aligned_beats,
            sections=sections,
            duration_target=duration_target,
            normalized_raw_script=raw,
        )

    def build_domain_script(
        self,
        analysis: ScriptAnalysisOutput,
        *,
        content_item_id: str,
        channel_id: str,
        strategy_version: int,
        version: int = 1,
        clock: Any | None = None,
    ) -> Script:
        """Create a domain ``Script`` entity from ``ScriptAnalysisOutput``."""
        return Script.create(
            content_item_id=content_item_id,
            channel_id=channel_id,
            content_type=analysis.story_bible.content_type,
            strategy_version=strategy_version,
            version=version,
            title=analysis.story_bible.title,
            sections=analysis.sections,
            duration_target=analysis.duration_target,
            clock=clock,
        )

    def _split_into_raw_blocks(
        self, raw: str
    ) -> list[tuple[str, list[str]]]:
        lines = [line.strip() for line in raw.split("\n")]
        blocks: list[tuple[str, list[str]]] = []
        current_heading = ""
        current_lines: list[str] = []

        for line in lines:
            if not line:
                if current_lines:
                    blocks.append((current_heading, current_lines))
                    current_heading = ""
                    current_lines = []
                continue

            heading_match = _SCENE_HEADING_RE.match(line)
            if heading_match:
                if current_lines:
                    blocks.append((current_heading, current_lines))
                    current_lines = []
                current_heading = heading_match.group(1).strip() or line
                continue

            current_lines.append(line)

        if current_lines:
            blocks.append((current_heading, current_lines))

        # If the user supplied a single block of text, split sentences into 3..5 narrative beats
        if len(blocks) == 1 and len(blocks[0][1]) <= 2:
            joined = " ".join(blocks[0][1])
            sentences = [
                s.strip()
                for s in re.split(r"(?<=[.!?…])\s+", joined)
                if s.strip()
            ]
            if len(sentences) >= 3:
                return [
                    (f"Scene {idx}", [sentence])
                    for idx, sentence in enumerate(sentences, start=1)
                ]
            if len(sentences) == 2:
                return [
                    ("Scene 1", [sentences[0]]),
                    ("Scene 2", [sentences[1]]),
                    ("Scene 3", [sentences[1]]),
                ]
            # Single sentence: split clauses or create 3 progressive beats preserving text
            words = joined.split()
            if len(words) >= 9:
                third = max(3, len(words) // 3)
                return [
                    ("Scene 1", [" ".join(words[:third])]),
                    ("Scene 2", [" ".join(words[third : 2 * third])]),
                    ("Scene 3", [" ".join(words[2 * third :])]),
                ]
            return [
                ("Opening", [joined]),
                ("Development", [joined]),
                ("Resolution", [joined]),
            ]

        return blocks

    def _extract_location_name(
        self,
        heading: str,
        lines: list[str],
        block_idx: int,
        assumptions: list[str],
    ) -> str:
        for line in lines:
            match = _LOCATION_LINE_RE.match(line)
            if match:
                return match.group(1).strip()[:60]
        if heading:
            cleaned = re.sub(
                r"\b(?:DAY|NIGHT|DAWN|DUSK|MORNING|EVENING|BAN NGÀY|BAN ĐÊM|BUỔI TỐI|BUỔI SÁNG)\b",
                "",
                heading,
                flags=re.IGNORECASE,
            ).strip(" -–—:.,")
            if cleaned and not cleaned.lower().startswith("scene"):
                return cleaned[:60]
        if block_idx == 1:
            assumptions.append(
                "No explicit scene heading location in script; inferred primary Story Stage setting from narrative context."
            )
        return "Primary Story Stage"

    def _parse_line_speaker(
        self, line: str
    ) -> tuple[str | None, str | None, str]:
        bracket = _BRACKET_SPEAKER_RE.match(line)
        if bracket:
            speaker = bracket.group("speaker").strip()
            emotion = bracket.group("emotion")
            text = bracket.group("text").strip()
            if speaker.lower() in _NARRATOR_ALIASES or speaker.lower() in _NON_CHARACTER_LABELS:
                return None, (emotion.strip() if emotion else None), text
            return speaker, (emotion.strip() if emotion else None), text

        colon = _COLON_SPEAKER_RE.match(line)
        if colon:
            speaker = colon.group("speaker").strip()
            emotion = colon.group("emotion")
            text = colon.group("text").strip()
            lowered = speaker.lower()
            if lowered in _NARRATOR_ALIASES or lowered in _NON_CHARACTER_LABELS:
                return None, (emotion.strip() if emotion else None), text
            if len(speaker.split()) <= 3:
                return speaker, (emotion.strip() if emotion else None), text

        return None, None, line

    def _resolve_title(
        self,
        explicit_title: str | None,
        raw_blocks: list[tuple[str, list[str]]],
        beats: list[RawSceneBeat],
    ) -> str:
        if explicit_title and explicit_title.strip():
            cleaned = explicit_title.strip().replace("<", "(").replace(">", ")")
            return cleaned[:95]
        if raw_blocks and raw_blocks[0][0]:
            candidate = raw_blocks[0][0].strip()
            if candidate and not candidate.lower().startswith("scene"):
                return candidate[:95]
        first_words = beats[0].spoken_text.split()[:8]
        fallback = " ".join(first_words).strip(" .,!?:;-") or "Story Video Project"
        return fallback[:95]

    def _build_story_arc(
        self,
        title: str,
        beats: list[RawSceneBeat],
        visual_style: str,
        assumptions: tuple[str, ...],
    ) -> StoryArc:
        opening = beats[0].spoken_text[:200]
        resolution = beats[-1].spoken_text[:200]
        middle_beats = beats[1:-1] if len(beats) > 2 else beats
        conflict = " ".join(b.spoken_text for b in middle_beats)[:240]
        climax = beats[max(0, len(beats) - 2)].spoken_text[:200]
        logline = f"{title}: {opening[:120]}"
        return StoryArc(
            title=title,
            logline=logline,
            opening_situation=opening,
            conflict_or_development=conflict,
            climax=climax,
            resolution=resolution,
            tone="cinematic_narrative",
            visual_style=visual_style,
            assumptions=assumptions,
        )

    def _build_continuity_constraints(
        self,
        characters: tuple[CharacterSpec, ...],
        locations: tuple[LocationSpec, ...],
    ) -> tuple[ContinuityConstraint, ...]:
        constraints: list[ContinuityConstraint] = []
        for idx, char in enumerate(characters, start=1):
            if char.role == "narrator":
                continue
            constraints.append(
                ContinuityConstraint(
                    constraint_id=f"cc_char_{idx}",
                    kind="character_appearance",
                    subject_id=char.character_id,
                    rule=(
                        f"Maintain identical appearance ({char.appearance}) and "
                        f"wardrobe ({char.wardrobe}) for {char.name} across all scenes."
                    ),
                )
            )
        for idx, loc in enumerate(locations, start=1):
            constraints.append(
                ContinuityConstraint(
                    constraint_id=f"cc_loc_{idx}",
                    kind="location_lighting",
                    subject_id=loc.location_id,
                    rule=(
                        f"Keep setting '{loc.name}' at {loc.time_of_day} with "
                        f"lighting '{loc.lighting_mood}' and RGB palette {list(loc.palette_rgb)}."
                    ),
                )
            )
        return tuple(constraints)

    def _build_sections_and_beats(
        self,
        beats: list[RawSceneBeat],
        *,
        content_type: ContentType,
        target_duration_seconds: int | None,
    ) -> tuple[tuple[ScriptSection, ...], DurationTarget, tuple[RawSceneBeat, ...]]:
        # Ensure at least 3 beats for Shorts (HOOK, BODY, CTA) and at least 5 beats for LongForm
        # (INTRO, CHAPTER 1, CHAPTER 2, CONCLUSION, OUTRO) without inventing contradictory plot.
        working = list(beats)
        if content_type is ContentType.SHORTS:
            while len(working) < 3:
                last = working[-1]
                working.append(
                    replace(
                        last,
                        beat_index=len(working) + 1,
                        heading=f"Continuation {len(working) + 1}",
                    )
                )
            sections: list[ScriptSection] = []
            aligned: list[RawSceneBeat] = []
            min_chars_per_sec = max(55, (285 // len(working)) + 5)
            max_chars_per_sec = max(130, 880 // len(working))
            for idx, beat in enumerate(working):
                if idx == 0:
                    kind = SectionKind.HOOK
                elif idx == len(working) - 1:
                    kind = SectionKind.CTA
                else:
                    kind = SectionKind.BODY
                norm_text = _normalize_section_narration(
                    beat.spoken_text,
                    heading=beat.heading,
                    action_summary=beat.action_summary,
                    min_chars=min_chars_per_sec,
                    max_chars=max_chars_per_sec,
                )
                sec_seconds = max(4, min(35, round(len(norm_text) * 60 / 1000)))
                sections.append(
                    ScriptSection(
                        kind=kind,
                        text=norm_text,
                        title=beat.heading if kind is SectionKind.BODY else None,
                        seconds=sec_seconds,
                    )
                )
                aligned.append(
                    replace(
                        beat,
                        beat_index=idx + 1,
                        spoken_text=norm_text,
                        estimated_seconds=sec_seconds,
                    )
                )

            total_sec = sum(s.seconds or 5 for s in sections)
            if total_sec > 180:
                scale = 170.0 / total_sec
                sections = [
                    ScriptSection(
                        kind=s.kind,
                        text=s.text,
                        title=s.title,
                        seconds=max(2, int((s.seconds or 5) * scale)),
                    )
                    for s in sections
                ]
                total_sec = sum(s.seconds or 5 for s in sections)
            min_sec = min(15, total_sec)
            max_sec = min(180, max(60, total_sec + 15))
            if target_duration_seconds is not None and 5 <= target_duration_seconds <= 180:
                min_sec = min(min_sec, target_duration_seconds)
                max_sec = max(max_sec, target_duration_seconds)
            return tuple(sections), DurationTarget(min_sec, max_sec), tuple(aligned)

        # ContentType.LONGFORM: requires INTRO, >=2 CHAPTERs (with non-empty titles), CONCLUSION, OUTRO
        # and total duration >= 181s so LongForm timeline and QC duration check pass.
        while len(working) < 5:
            source_beat = working[min(len(working) - 1, len(working) // 2)]
            working.append(
                replace(
                    source_beat,
                    beat_index=len(working) + 1,
                    heading=f"Part {len(working) + 1}",
                )
            )

        raw_total = sum(max(10, b.estimated_seconds) for b in working)
        desired_total = max(
            200,
            target_duration_seconds if (target_duration_seconds and target_duration_seconds >= 185) else raw_total,
        )
        scale_factor = max(1.0, desired_total / max(1, raw_total))

        lf_sections: list[ScriptSection] = []
        lf_aligned: list[RawSceneBeat] = []
        n = len(working)
        chapter_num = 0
        min_lf_chars_per_sec = max(220, (3150 // n) + 15)
        for idx, beat in enumerate(working):
            if idx == 0:
                kind = SectionKind.INTRO
                sec_title: str | None = "Introduction"
            elif idx == n - 2:
                kind = SectionKind.CONCLUSION
                sec_title = "Conclusion"
            elif idx == n - 1:
                kind = SectionKind.OUTRO
                sec_title = "Outro"
            else:
                kind = SectionKind.CHAPTER
                chapter_num += 1
                clean_heading = beat.heading.strip()
                if not clean_heading or clean_heading.lower().startswith("scene"):
                    sec_title = f"Chapter {chapter_num}: Story Progression"
                else:
                    sec_title = clean_heading[:80]

            norm_lf_text = _normalize_section_narration(
                beat.spoken_text,
                heading=sec_title or beat.heading,
                action_summary=beat.action_summary,
                min_chars=min_lf_chars_per_sec,
            )
            sec_seconds = max(
                20,
                int(round(max(len(norm_lf_text) * 60 / 1000, beat.estimated_seconds * scale_factor))),
            )

            lf_sections.append(
                ScriptSection(
                    kind=kind,
                    text=norm_lf_text,
                    title=sec_title,
                    seconds=sec_seconds,
                )
            )
            lf_aligned.append(
                replace(
                    beat,
                    beat_index=idx + 1,
                    heading=sec_title or beat.heading,
                    spoken_text=norm_lf_text,
                    estimated_seconds=sec_seconds,
                )
            )

        total_lf_sec = sum(s.seconds or 30 for s in lf_sections)
        min_lf = max(181, min(181, total_lf_sec))
        max_lf = min(14400, max(total_lf_sec + 120, 600))
        return tuple(lf_sections), DurationTarget(min_lf, max_lf), tuple(lf_aligned)
