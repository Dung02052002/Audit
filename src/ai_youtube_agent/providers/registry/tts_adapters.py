"""TTS Provider Adapters.

Implements unified adapters for:
1. EdgeTTSAdapter: Free, high-fidelity Vietnamese neural voices. Calls the real
   Edge online service (optional ``edge-tts`` extra, internet required); there is
   no silent mock fallback.
2. ElevenLabsTTSAdapter: Optional premium cloud speech synthesis (paid). There
   is no real HTTP integration yet: it refuses to synthesize and never returns
   mock audio in place of ElevenLabs audio (BUDGET-01).
3. MockTTSAdapter: Fast, offline, deterministic fallback generator.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_youtube_agent.core.log import get_logger
from ai_youtube_agent.providers.media_probe import MediaProbeError, probe_mp3_bytes
from ai_youtube_agent.providers.mock_speech_synthesis import MockSpeechSynthesizer
from ai_youtube_agent.providers.provider_cost import (
    ProviderCostClass,
    ProviderCostDeclaration,
    free_declaration,
    paid_declaration,
    with_fallback,
)
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
from ai_youtube_agent.providers.speech_synthesis import (
    SpeechRequest,
    SpeechSynthesisError,
    SpeechSynthesizer,
    SynthesizedSpeech,
    VoiceErrorCode,
)

logger = get_logger(__name__)

# Standard high-quality neural Vietnamese voices
DEFAULT_VIETNAMESE_VOICES = (
    "vi-VN-HoaiMyNeural",  # Female, clear, professional storytelling
    "vi-VN-NamMinhNeural",  # Male, deep, documentary / commentary
)

EDGE_TTS_INSTALL_COMMAND = "uv sync --extra tts"
EDGE_VOICE_PATTERN = re.compile(
    r"[a-z]{2,3}-[A-Z]{2,}(-[A-Za-z0-9]+)?-[A-Za-z0-9]+Neural"
)
CommunicateFactory = Callable[..., Any]

_MISSING_LIBRARY_MESSAGE = (
    "The edge-tts library is not installed; run `uv sync --extra tts`. "
    "Edge TTS also requires internet access."
)
# Provider errors for which an explicit fallback synthesizer may be used.
_FALLBACK_CODES = frozenset(
    {
        VoiceErrorCode.UNAVAILABLE,
        VoiceErrorCode.TIMEOUT,
        VoiceErrorCode.RATE_LIMITED,
    }
)


class EdgeTTSAdapter(BaseProviderAdapter, SpeechSynthesizer):
    """Free neural TTS provider with excellent Vietnamese voice quality.

    Calls the real Microsoft Edge online TTS service through the optional
    ``edge-tts`` library (``uv sync --extra tts``); it needs internet access and
    no API key. It never returns audio that Edge did not produce: failures raise
    ``SpeechSynthesisError``. A ``fallback`` synthesizer is used only when the
    caller passes one explicitly, and only for provider errors (unavailable,
    timeout, rate limited), never for a bad voice or a refused request. The
    fallback's result keeps the fallback's own provider name.
    """

    def __init__(
        self,
        *,
        default_voice: str = "vi-VN-HoaiMyNeural",
        timeout_seconds: float = 8.0,
        overall_timeout_seconds: float = 60.0,
        output_format: str = "audio/mpeg",
        ffmpeg_binary: str = "ffmpeg",
        fallback: SpeechSynthesizer | None = None,
        communicate_factory: CommunicateFactory | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if overall_timeout_seconds <= 0:
            raise ValueError("overall_timeout_seconds must be positive")
        if output_format not in ("audio/mpeg", "audio/wav"):
            raise ValueError("output_format must be 'audio/mpeg' or 'audio/wav'")
        super().__init__(
            provider_id="edge_tts",
            category=ProviderCategory.SPEECH_SYNTHESIS,
        )
        self.default_voice = default_voice
        self.timeout_seconds = timeout_seconds
        self.overall_timeout_seconds = overall_timeout_seconds
        self.output_format = output_format
        self.ffmpeg_binary = ffmpeg_binary
        self.name = "edge_tts"
        self._fallback = fallback
        self._communicate_factory = communicate_factory

    def with_wav_output(self, *, ffmpeg_binary: str | None = None) -> EdgeTTSAdapter:
        """Return an ``EdgeTTSAdapter`` that transcodes real Edge MP3 into PCM WAV."""
        return EdgeTTSAdapter(
            default_voice=self.default_voice,
            timeout_seconds=self.timeout_seconds,
            overall_timeout_seconds=self.overall_timeout_seconds,
            output_format="audio/wav",
            ffmpeg_binary=ffmpeg_binary or self.ffmpeg_binary,
            fallback=self._fallback,
            communicate_factory=self._communicate_factory,
        )

    @property
    def fallback_enabled(self) -> bool:
        return self._fallback is not None

    def cost_declaration(self) -> ProviderCostDeclaration:
        """A free online service; a non-free explicit fallback makes it paid."""
        return with_fallback(
            free_declaration(
                self.name,
                self.default_voice,
                ProviderCostClass.FREE_SERVICE,
                "Dịch vụ Microsoft Edge TTS trực tuyến miễn phí, không cần API key "
                "(không phải cục bộ).",
            ),
            self._fallback,
        )

    def capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            provider_id=self.provider_id,
            name="Microsoft Edge Neural TTS (Vietnamese Priority)",
            category=self.category,
            is_local=False,
            is_free=True,
            supported_modalities=(
                "speech_synthesis",
                "vietnamese_neural",
                "multilingual",
            ),
            supported_models=DEFAULT_VIETNAMESE_VOICES
            + ("en-US-JennyNeural", "en-US-GuyNeural"),
            resource_requirements=ResourceRequirement(
                ram_mb_min=512,
                disk_space_mb_min=100,
                requires_internet=True,
            ),
            metadata={
                "priority": "local_free_vietnamese",
                "default_voice": self.default_voice,
                "network": "internet_required",
                "offline": False,
                "output_format": "audio/mpeg",
                "install": EDGE_TTS_INSTALL_COMMAND,
                "library_installed": _edge_tts_installed(),
            },
        )

    def check_connection(self) -> ConnectionCheckResult:
        # Probe Edge TTS service endpoint
        start = time.monotonic()
        endpoint = "https://speech.platform.bing.com"
        try:
            req = urllib.request.Request(
                endpoint,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            # A 200 or 404/405 means endpoint is network reachable
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds):
                    pass
            except urllib.error.HTTPError:
                pass  # Server responded, socket connected

            latency_ms = (time.monotonic() - start) * 1000.0
            return ConnectionCheckResult(
                connected=True,
                latency_ms=round(latency_ms, 2),
                endpoint=endpoint,
            )
        except Exception as exc:
            return ConnectionCheckResult(
                connected=False,
                endpoint=endpoint,
                error_message=f"Edge TTS requires internet access: {exc}",
            )

    def check_models(self) -> ModelCheckResult:
        if self._communicate_factory is None and not _edge_tts_installed():
            return ModelCheckResult(
                valid=False,
                available_models=DEFAULT_VIETNAMESE_VOICES,
                default_model=self.default_voice,
                error_message=_MISSING_LIBRARY_MESSAGE,
            )
        return ModelCheckResult(
            valid=True,
            available_models=DEFAULT_VIETNAMESE_VOICES,
            default_model=self.default_voice,
        )

    def check_credentials(self) -> CredentialCheckResult:
        return CredentialCheckResult(
            valid=True,
            has_credentials=True,
            auth_type="none",
        )

    def check_resources(self) -> ResourceCheckResult:
        return ResourceCheckResult(
            sufficient=True,
            details={"requires_internet": True},
        )

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        """Synthesize speech with the real Edge TTS service.

        Raises ``SpeechSynthesisError`` when Edge TTS cannot deliver; the optional
        explicit fallback is used only for UNAVAILABLE, TIMEOUT and RATE_LIMITED.
        """
        try:
            return self._synthesize_with_edge(request)
        except SpeechSynthesisError as exc:
            if self._fallback is None or exc.voice_code not in _FALLBACK_CODES:
                raise
            logger.warning(
                "Edge TTS failed, using the explicit fallback synthesizer",
                extra={
                    "fields": {
                        "error_code": exc.voice_code.value,
                        "fallback": getattr(self._fallback, "name", "unknown"),
                    }
                },
            )
            return self._fallback.synthesize(request)

    def _synthesize_with_edge(self, request: SpeechRequest) -> SynthesizedSpeech:
        if not EDGE_VOICE_PATTERN.fullmatch(request.voice_id):
            raise SpeechSynthesisError(
                VoiceErrorCode.VOICE_NOT_FOUND,
                f"'{request.voice_id}' is not an Edge neural voice name "
                "(for example vi-VN-HoaiMyNeural).",
                provider=self.name,
            )
        if request.speaking_style is not None:
            logger.debug("Edge TTS ignores speaking_style")

        factory = self._resolve_factory()
        try:
            communicate = factory(
                request.text,
                request.voice_id,
                rate="+0%",
                volume="+0%",
                pitch="+0Hz",
                connect_timeout=max(1, int(self.timeout_seconds)),
                receive_timeout=max(1, int(self.overall_timeout_seconds)),
            )
        except (ValueError, TypeError) as exc:
            raise SpeechSynthesisError(
                VoiceErrorCode.INVALID_REQUEST,
                "Edge TTS rejected the synthesis parameters "
                f"({_safe_detail(exc, request.text)}).",
                provider=self.name,
            ) from exc
        except Exception as exc:
            raise self._map_failure(exc, request) from exc

        try:
            audio = self._run_stream(communicate)
        except Exception as exc:
            raise self._map_failure(exc, request) from exc

        if not audio:
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE,
                "Edge TTS answered but no audio was produced.",
                provider=self.name,
            )
        try:
            probe = probe_mp3_bytes(audio)
        except MediaProbeError as exc:
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE,
                f"Edge TTS audio is not decodable MP3: {exc}",
                provider=self.name,
            ) from exc
        if self.output_format == "audio/wav":
            wav_audio, wav_duration_ms = self._transcode_mp3_to_wav(audio)
            return SynthesizedSpeech(
                audio=wav_audio,
                media_type="audio/wav",
                duration_ms=wav_duration_ms,
                provider=self.name,
                voice_id=request.voice_id,
                characters=len(request.text),
                generated_at=datetime.now(UTC),
            )
        return SynthesizedSpeech(
            audio=audio,
            media_type="audio/mpeg",
            duration_ms=probe.duration_ms,
            provider=self.name,
            voice_id=request.voice_id,
            characters=len(request.text),
            generated_at=datetime.now(UTC),
        )

    def synthesize_wav(self, request: SpeechRequest) -> SynthesizedSpeech:
        """Synthesize real Edge TTS speech and return 24 kHz mono s16 PCM WAV."""
        return self.with_wav_output().synthesize(request)

    def synthesize_to_file(
        self, request: SpeechRequest, output_path: Path | str
    ) -> tuple[SynthesizedSpeech, Path]:
        """Synthesize real Edge TTS audio, write to ``output_path``, and verify."""
        speech = self.synthesize(request)
        target = Path(output_path).resolve()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(speech.audio)
            if not target.exists():
                raise OSError(f"output file {target} was not created")
            read_back = target.read_bytes()
            if not read_back:
                raise ValueError(f"output file {target} is empty")
            if speech.media_type == "audio/mpeg":
                probe_mp3_bytes(read_back)
            else:
                self._verify_wav_bytes(read_back)
        except Exception as exc:
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE,
                f"Edge TTS output file verification failed ({type(exc).__name__}).",
                provider=self.name,
            ) from exc
        return speech, target

    def _transcode_mp3_to_wav(self, mp3_bytes: bytes) -> tuple[bytes, int]:
        try:
            proc = subprocess.run(
                [
                    self.ffmpeg_binary,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "mp3",
                    "-i",
                    "pipe:0",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    "-f",
                    "s16le",
                    "-acodec",
                    "pcm_s16le",
                    "pipe:1",
                ],
                input=mp3_bytes,
                capture_output=True,
                timeout=max(5.0, self.overall_timeout_seconds),
                check=False,
            )
        except Exception as exc:
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE,
                f"ffmpeg transcoding of Edge TTS audio failed ({type(exc).__name__}).",
                provider=self.name,
            ) from exc
        pcm = proc.stdout
        if proc.returncode != 0 or not pcm:
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE,
                "ffmpeg could not decode Edge TTS MP3 audio into PCM WAV.",
                provider=self.name,
            )
        if len(pcm) % 2 != 0:
            pcm = pcm[:-1]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(pcm)
        wav_bytes = buf.getvalue()
        duration_ms = self._verify_wav_bytes(wav_bytes)
        return wav_bytes, duration_ms

    def _verify_wav_bytes(self, wav_bytes: bytes) -> int:
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
                if reader.getcomptype() != "NONE":
                    raise ValueError("WAV is not uncompressed PCM")
                frames = reader.getnframes()
                rate = reader.getframerate()
                width = reader.getsampwidth()
                channels = reader.getnchannels()
                raw = reader.readframes(frames)
        except Exception as exc:
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE,
                f"Edge TTS WAV output is corrupt ({type(exc).__name__}).",
                provider=self.name,
            ) from exc
        expected_bytes = frames * width * channels
        if frames <= 0 or rate <= 0 or len(raw) != expected_bytes:
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE,
                "Edge TTS WAV output has zero or truncated PCM frames.",
                provider=self.name,
            )
        return max(1, round(frames * 1000 / rate))

    def _resolve_factory(self) -> CommunicateFactory:
        if self._communicate_factory is not None:
            return self._communicate_factory
        try:
            import edge_tts
        except ImportError as exc:
            error = SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE,
                _MISSING_LIBRARY_MESSAGE,
                provider=self.name,
            )
            # Installing the extra is a human step: retrying cannot fix it.
            error.retryable = False
            raise error from exc
        return edge_tts.Communicate

    def _run_stream(self, communicate: Any) -> bytes:
        """Collect the audio chunks of ``communicate`` within the overall timeout."""

        async def collect() -> bytes:
            chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if isinstance(chunk, dict) and chunk.get("type") == "audio":
                    data = chunk.get("data")
                    if isinstance(data, bytes | bytearray):
                        chunks.append(bytes(data))
            return b"".join(chunks)

        async def bounded() -> bytes:
            return await asyncio.wait_for(
                collect(), timeout=self.overall_timeout_seconds
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(bounded())
        # Called from inside a running event loop: use a private loop in a thread.
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, bounded()).result()

    def _map_failure(
        self, exc: Exception, request: SpeechRequest
    ) -> SpeechSynthesisError:
        code, reason = _classify(exc)
        return SpeechSynthesisError(
            code,
            f"{reason} ({_safe_detail(exc, request.text)})",
            provider=self.name,
        )


def _edge_tts_installed() -> bool:
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except (ImportError, ValueError):
        return False


def _safe_detail(exc: BaseException, text: str) -> str:
    """Exception type and message, never containing the request text."""
    try:
        message = " ".join(str(exc).split())[:200]
    except Exception:
        message = ""
    if text and text in message:
        message = "details withheld"
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _classify(exc: BaseException) -> tuple[VoiceErrorCode, str]:
    """Map an Edge TTS / network exception to a voice error code and a reason."""
    internet = "Edge TTS requires internet access"
    if isinstance(exc, TimeoutError):
        return VoiceErrorCode.TIMEOUT, "Edge TTS did not answer in time"
    try:
        import aiohttp
    except ImportError:
        aiohttp = None
    try:
        from edge_tts import exceptions as edge_exceptions
    except ImportError:
        edge_exceptions = None

    if aiohttp is not None:
        if isinstance(exc, aiohttp.ClientResponseError):
            if exc.status == 429:
                return VoiceErrorCode.RATE_LIMITED, "Edge TTS rate limit reached"
            return (
                VoiceErrorCode.UNAVAILABLE,
                f"Edge TTS answered HTTP {exc.status}; {internet}",
            )
        if isinstance(exc, aiohttp.ClientError):
            return VoiceErrorCode.UNAVAILABLE, f"Edge TTS is unreachable; {internet}"
    if edge_exceptions is not None:
        if isinstance(exc, edge_exceptions.NoAudioReceived):
            return VoiceErrorCode.REFUSED, "Edge TTS returned no audio for this request"
        service_errors = (
            edge_exceptions.WebSocketError,
            edge_exceptions.SkewAdjustmentError,
            edge_exceptions.UnknownResponse,
            edge_exceptions.UnexpectedResponse,
        )
        if isinstance(exc, service_errors):
            return VoiceErrorCode.UNAVAILABLE, f"Edge TTS service error; {internet}"
    if isinstance(exc, OSError):
        return VoiceErrorCode.UNAVAILABLE, f"Edge TTS is unreachable; {internet}"
    return VoiceErrorCode.UNAVAILABLE, "Edge TTS failed unexpectedly"


class ElevenLabsTTSAdapter(BaseProviderAdapter, SpeechSynthesizer):
    """Optional premium cloud voice provider (paid).

    There is no real ElevenLabs HTTP integration yet, so ``synthesize`` always
    raises ``SpeechSynthesisError`` and never answers with mock audio. Its cost
    declaration is ``paid`` without a verified price, so the budget guard blocks
    it before any call (BUDGET-01).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_voice_id: str = "21m00Tcm4TlvDq8ikWAM",  # Rachel
        model_id: str = "eleven_multilingual_v2",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            provider_id="elevenlabs",
            category=ProviderCategory.SPEECH_SYNTHESIS,
        )
        self.api_key = api_key
        self.default_voice_id = default_voice_id
        self.model_id = model_id
        self.timeout_seconds = timeout_seconds
        self.name = "elevenlabs"

    def cost_declaration(self) -> ProviderCostDeclaration:
        return paid_declaration(
            self.name,
            self.model_id,
            "API ElevenLabs tính phí theo số ký tự; chưa có nguồn giá đã xác minh.",
        )

    def capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            provider_id=self.provider_id,
            name="ElevenLabs Cloud Voice Engine",
            category=self.category,
            is_local=False,
            is_free=False,
            supported_modalities=("speech_synthesis", "voice_cloning", "multilingual"),
            supported_models=(
                "eleven_multilingual_v2",
                "eleven_turbo_v2_5",
                "eleven_flash_v2_5",
            ),
            resource_requirements=ResourceRequirement(
                ram_mb_min=512,
                disk_space_mb_min=100,
                requires_internet=True,
            ),
            metadata={"model_id": self.model_id},
        )

    def check_connection(self) -> ConnectionCheckResult:
        if not self.api_key:
            return ConnectionCheckResult(
                connected=False,
                endpoint="https://api.elevenlabs.io",
                error_message="ElevenLabs API key not configured.",
            )

        start = time.monotonic()
        endpoint = "https://api.elevenlabs.io/v1/user"
        try:
            req = urllib.request.Request(
                endpoint,
                headers={"xi-api-key": self.api_key},
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds):
                pass
            latency_ms = (time.monotonic() - start) * 1000.0
            return ConnectionCheckResult(
                connected=True,
                latency_ms=round(latency_ms, 2),
                endpoint=endpoint,
            )
        except Exception as exc:
            return ConnectionCheckResult(
                connected=False,
                endpoint=endpoint,
                error_message=f"ElevenLabs connection failed: {exc}",
            )

    def check_models(self) -> ModelCheckResult:
        return ModelCheckResult(
            valid=True,
            available_models=("eleven_multilingual_v2", "eleven_turbo_v2_5"),
            default_model=self.model_id,
        )

    def check_credentials(self) -> CredentialCheckResult:
        if not self.api_key:
            return CredentialCheckResult(
                valid=False,
                has_credentials=False,
                auth_type="api_key",
                error_message="ELEVENLABS_API_KEY environment variable is missing.",
            )
        return CredentialCheckResult(
            valid=True,
            has_credentials=True,
            auth_type="api_key",
        )

    def check_resources(self) -> ResourceCheckResult:
        return ResourceCheckResult(
            sufficient=True,
            details={"requires_internet": True},
        )

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        if not self.api_key:
            raise SpeechSynthesisError(
                VoiceErrorCode.UNAVAILABLE,
                "ElevenLabs is not configured with an API key.",
                provider=self.name,
            )
        raise SpeechSynthesisError(
            VoiceErrorCode.UNAVAILABLE,
            "ElevenLabs chưa có tích hợp HTTP thật; không tạo giọng giả.",
            provider=self.name,
        )


class MockTTSAdapter(BaseProviderAdapter, SpeechSynthesizer):
    """Deterministic, offline speech synthesis adapter."""

    def __init__(self, synthesizer: MockSpeechSynthesizer | None = None) -> None:
        super().__init__(
            provider_id="mock_tts",
            category=ProviderCategory.SPEECH_SYNTHESIS,
        )
        self._synth = synthesizer or MockSpeechSynthesizer()
        self.name = "mock"

    def cost_declaration(self) -> ProviderCostDeclaration:
        return free_declaration(
            self.name,
            "mock-vietnamese-1",
            ProviderCostClass.MOCK,
            "Giọng mock trong bộ nhớ.",
        )

    def capabilities(self) -> ProviderCapability:
        return ProviderCapability(
            provider_id=self.provider_id,
            name="Deterministic Mock Speech Synthesizer",
            category=self.category,
            is_local=True,
            is_free=True,
            supported_modalities=("speech_synthesis", "offline_mock"),
            supported_models=("mock-vietnamese-1", "mock-english-1"),
        )

    def check_connection(self) -> ConnectionCheckResult:
        return ConnectionCheckResult(
            connected=True, latency_ms=0.1, endpoint="in-memory"
        )

    def check_models(self) -> ModelCheckResult:
        return ModelCheckResult(
            valid=True,
            available_models=("mock-vietnamese-1",),
            default_model="mock-vietnamese-1",
        )

    def check_credentials(self) -> CredentialCheckResult:
        return CredentialCheckResult(valid=True, has_credentials=True, auth_type="none")

    def check_resources(self) -> ResourceCheckResult:
        return ResourceCheckResult(sufficient=True)

    def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        return self._synth.synthesize(request)
