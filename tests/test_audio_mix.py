"""I-099 audio mix engine (Prompt Pack v8, prompt #099).

Rules decided on 2026-10-17 (``content/audio_mix.py``):

- only PCM WAV of 8/16/24/32 bits, 1 or 2 channels and 8000..192000 Hz is read;
- the output has the voice's rate, channels and exact length; other tracks are
  converted (channels, linear resampling); nothing is dropped silently;
- gain is the track's or the role default, clamped; fades are raised cosine;
- music (not effects) is ducked under the active voice with attack, release and
  look-ahead; the level goes to the RMS target with a capped static gain; a
  look-ahead limiter keeps the peak under the ceiling; the result is
  deterministic 16-bit PCM.
"""

import io
import math
import struct
import wave
from array import array

import pytest

from ai_youtube_agent.content.audio_mix import (
    DEFAULT_CONSTRAINTS,
    MixConstraints,
    MixInputError,
    MixRole,
    MixTrack,
    PcmAudio,
    decode_wav,
    encode_wav,
    mix_audio,
)

RATE = 8000
FULL = 32768


def pcm_of(values, rate: int = RATE, channels: int = 1) -> PcmAudio:
    return PcmAudio(rate, channels, array("d", values))


def tone(ms: int, amp: float = 0.3, freq: float = 200, rate: int = RATE, ch: int = 1):
    frames = rate * ms // 1000
    values = []
    for index in range(frames):
        value = amp * math.sin(2 * math.pi * freq * index / rate)
        values.extend([value] * ch)
    return pcm_of(values, rate, ch)


def dc(ms: int, level: float, rate: int = RATE, ch: int = 1) -> PcmAudio:
    return pcm_of([level] * (rate * ms // 1000 * ch), rate, ch)


def silence(ms: int, rate: int = RATE, ch: int = 1) -> PcmAudio:
    return dc(ms, 0.0, rate, ch)


def track(role=MixRole.SFX, pcm=None, **kwargs) -> MixTrack:
    kwargs.setdefault("label", "t1")
    return MixTrack(role, pcm or dc(1000, 0.5), **kwargs)


def out_of(result) -> PcmAudio:
    return decode_wav(result.wav)


def sample_at(result, ms: int) -> float:
    pcm = out_of(result)
    return pcm.samples[ms * pcm.sample_rate // 1000 * pcm.channels]


FLAT = MixConstraints(max_boost_db=0)  # no static gain: the shape is easy to read


def raw_wav(width: int, raw: bytes, channels: int = 1, rate: int = RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(raw)
    return buffer.getvalue()


# Codec


def test_8_bit_wav_is_unsigned_and_scaled() -> None:
    pcm = decode_wav(raw_wav(1, bytes([0, 128, 255])))

    assert list(pcm.samples) == [-1.0, 0.0, 127 / 128]


def test_16_bit_wav() -> None:
    pcm = decode_wav(raw_wav(2, struct.pack("<hhh", -32768, 0, 16384)))

    assert list(pcm.samples) == [-1.0, 0.0, 0.5]


def test_24_bit_wav() -> None:
    raw = b"".join(
        value.to_bytes(3, "little", signed=True) for value in (-8388608, 0, 4194304)
    )

    assert list(decode_wav(raw_wav(3, raw)).samples) == [-1.0, 0.0, 0.5]


def test_32_bit_wav() -> None:
    pcm = decode_wav(raw_wav(4, struct.pack("<iii", -(2**31), 0, 2**30)))

    assert list(pcm.samples) == [-1.0, 0.0, 0.5]


def test_stereo_wav_keeps_its_channels_interleaved() -> None:
    pcm = decode_wav(raw_wav(2, struct.pack("<hhhh", 100, -100, 200, -200), 2))

    assert pcm.channels == 2 and pcm.frames == 2
    assert pcm.samples[0] == -pcm.samples[1]


def test_16_bit_round_trips_exactly() -> None:
    data = raw_wav(2, struct.pack("<hhhh", -32768, -1, 12345, 32767))

    assert decode_wav(encode_wav(decode_wav(data))) == decode_wav(data)
    assert encode_wav(decode_wav(data)) == data


def test_encoding_rounds_half_to_even_and_clamps() -> None:
    pcm = pcm_of([0.5 / FULL, 1.5 / FULL, 2.0, -2.0])

    values = array("h")
    with wave.open(io.BytesIO(encode_wav(pcm))) as reader:
        values.frombytes(reader.readframes(4))

    assert list(values) == [0, 2, 32767, -32768]


def float_wav() -> bytes:
    fmt = struct.pack("<HHIIHH", 3, 1, RATE, RATE * 4, 4, 32)
    data = struct.pack("<f", 0.5)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a wav",
        b"RIFF\x00\x00\x00\x00WAVE",
        raw_wav(2, b"", 1),
        raw_wav(2, struct.pack("<hh", 1, 2), 1, 4000),
        raw_wav(2, struct.pack("<hh", 1, 2), 1, 200000),
        float_wav(),
    ],
    ids=["empty", "garbage", "headers only", "no frames", "low", "high", "float"],
)
def test_unreadable_or_unsupported_wav_is_refused(data) -> None:
    with pytest.raises(MixInputError):
        decode_wav(data)


def test_more_than_two_channels_is_refused() -> None:
    with pytest.raises(MixInputError):
        decode_wav(raw_wav(2, struct.pack("<hhh", 1, 2, 3), 3))


def test_a_file_over_the_byte_limit_is_refused() -> None:
    data = raw_wav(2, struct.pack("<hh", 1, 2))

    with pytest.raises(MixInputError):
        decode_wav(data, max_bytes=len(data) - 1)
    assert decode_wav(data, max_bytes=len(data)).frames == 2


def test_truncated_data_is_refused() -> None:
    data = raw_wav(2, struct.pack("<hhhh", 1, 2, 3, 4))

    with pytest.raises(MixInputError):
        decode_wav(data[:-3])


def test_pcm_needs_whole_frames_and_a_supported_rate() -> None:
    with pytest.raises(MixInputError):
        pcm_of([0.0, 0.0, 0.0], channels=2)
    with pytest.raises(MixInputError):
        pcm_of([], channels=1)
    with pytest.raises(MixInputError):
        pcm_of([0.0], rate=100)
    with pytest.raises(MixInputError):
        pcm_of([0.0], channels=3)


# Format and length


@pytest.mark.parametrize(("rate", "channels"), [(8000, 1), (16000, 2), (44100, 2)])
def test_output_has_the_format_and_exact_length_of_the_voice(rate, channels) -> None:
    frames = rate * 1234 // 1000 + 1  # an awkward length
    voice = pcm_of(
        [0.1 * ((i % 7) - 3) for i in range(frames * channels)], rate, channels
    )
    other = tone(900, rate=22050, ch=1)

    result = mix_audio(voice, [track(MixRole.MUSIC, other)])

    out = out_of(result)
    assert (out.sample_rate, out.channels, out.frames) == (rate, channels, frames)
    assert result.report.duration_ms == voice.duration_ms


def test_mono_track_is_duplicated_for_a_stereo_voice() -> None:
    voice = silence(1000, ch=2)

    result = mix_audio(voice, [track(pcm=dc(1000, 0.4), gain_db=0)], FLAT)

    out = out_of(result)
    assert out.channels == 2
    assert (
        out.samples[100 * 2] == out.samples[100 * 2 + 1] == pytest.approx(0.4, abs=1e-3)
    )


def test_stereo_track_is_averaged_for_a_mono_voice() -> None:
    stereo = pcm_of([0.6, 0.2] * 8000, channels=2)

    result = mix_audio(silence(1000), [track(pcm=stereo, gain_db=0)], FLAT)

    assert sample_at(result, 500) == pytest.approx(0.4, abs=1e-3)


def test_resampling_keeps_the_duration_and_the_level() -> None:
    result = mix_audio(
        silence(2000, rate=16000),
        [track(pcm=dc(1000, 0.4, rate=8000), gain_db=0, end_ms=1000)],
        FLAT,
    )

    assert sample_at(result, 500) == pytest.approx(0.4, abs=1e-3)
    assert sample_at(result, 1500) == 0.0
    assert result.report.tracks[0].end_ms == 1000


def test_linear_resampling_interpolates_between_samples() -> None:
    ramp = pcm_of([0.0, 0.5] * 4000, rate=8000)  # 1 s

    result = mix_audio(silence(1000, rate=16000), [track(pcm=ramp, gain_db=0)], FLAT)

    out = out_of(result)
    assert out.samples[1] == pytest.approx(0.25, abs=1e-3)


# Placement


def test_a_track_starts_where_it_is_placed() -> None:
    result = mix_audio(
        silence(2000), [track(pcm=dc(500, 0.4), gain_db=0, start_ms=1000)], FLAT
    )

    assert sample_at(result, 900) == 0.0
    assert sample_at(result, 1100) == pytest.approx(0.4, abs=1e-3)
    assert sample_at(result, 1600) == 0.0
    assert result.report.warnings == ()


def test_a_source_window_is_cut() -> None:
    values = [0.1] * 4000 + [0.4] * 4000
    result = mix_audio(
        silence(1000),
        [track(pcm=pcm_of(values), gain_db=0, source_in_ms=500, source_out_ms=900)],
        FLAT,
    )

    assert sample_at(result, 100) == pytest.approx(0.4, abs=1e-3)
    assert sample_at(result, 399) == pytest.approx(0.4, abs=1e-3)
    assert sample_at(result, 450) == 0.0


def test_a_source_window_past_the_audio_is_refused() -> None:
    with pytest.raises(MixInputError):
        mix_audio(silence(1000), [track(source_in_ms=5000)])


def test_a_short_music_source_is_not_looped_and_warns() -> None:
    result = mix_audio(
        silence(2000),
        [track(MixRole.MUSIC, dc(500, 0.4), gain_db=0, label="song")],
        FLAT,
    )

    assert sample_at(result, 400) == pytest.approx(0.4, abs=1e-3)
    assert sample_at(result, 600) == 0.0
    assert result.report.warnings == ("music_short:song",)


def test_a_short_effect_does_not_warn() -> None:
    result = mix_audio(silence(2000), [track(pcm=dc(500, 0.4))])

    assert result.report.warnings == ()


def test_a_track_past_the_voice_end_is_clipped_with_a_warning() -> None:
    result = mix_audio(
        silence(1000),
        [track(pcm=dc(5000, 0.4), end_ms=4000, gain_db=0, label="x")],
        FLAT,
    )

    assert out_of(result).frames == RATE
    assert result.report.warnings == ("track_clipped:x",)
    assert result.report.tracks[0].end_ms == 1000


def test_a_track_that_starts_after_the_voice_is_refused() -> None:
    with pytest.raises(MixInputError):
        mix_audio(silence(1000), [track(start_ms=1000)])


def test_a_track_must_end_after_it_starts() -> None:
    with pytest.raises(MixInputError):
        track(start_ms=500, end_ms=500)
    with pytest.raises(MixInputError):
        track(source_in_ms=500, source_out_ms=500)


def test_overlapping_tracks_add_up() -> None:
    result = mix_audio(
        silence(1000),
        [
            track(pcm=dc(600, 0.2), gain_db=0, label="a"),
            track(pcm=dc(600, 0.3), gain_db=0, start_ms=300, label="b"),
        ],
        FLAT,
    )

    assert sample_at(result, 100) == pytest.approx(0.2, abs=1e-3)
    assert sample_at(result, 450) == pytest.approx(0.5, abs=1e-3)
    assert sample_at(result, 800) == pytest.approx(0.3, abs=1e-3)


# Gain and fades


def test_role_default_gains() -> None:
    result = mix_audio(
        silence(1000),
        [
            track(MixRole.MUSIC, dc(1000, 0.5), label="m"),
            track(MixRole.SFX, dc(1000, 0.5), label="s"),
        ],
        FLAT,
    )

    gains = {item.label: item.gain_db for item in result.report.tracks}
    assert gains == {"m": -18.0, "s": -6.0}
    assert sample_at(result, 500) == pytest.approx(
        0.5 * 10 ** (-18 / 20) + 0.5 * 10 ** (-6 / 20), abs=1e-3
    )


def test_a_track_gain_is_clamped_to_the_constraint_range() -> None:
    constraints = MixConstraints(min_gain_db=-20, max_gain_db=3, max_boost_db=0)

    result = mix_audio(
        silence(1000),
        [track(gain_db=-40, label="low"), track(gain_db=9, label="high")],
        constraints,
    )

    assert [item.gain_db for item in result.report.tracks] == [-20.0, 3.0]


def test_a_track_gain_outside_the_global_range_is_refused() -> None:
    with pytest.raises(MixInputError):
        track(gain_db=13)
    with pytest.raises(MixInputError):
        track(gain_db=-61)
    track(gain_db=12)
    track(gain_db=-60)


def test_fades_are_raised_cosine() -> None:
    result = mix_audio(
        silence(1000),
        [track(pcm=dc(1000, 0.5), gain_db=0, fade_in_ms=100, fade_out_ms=100)],
        FLAT,
    )

    assert sample_at(result, 0) < 0.002
    assert sample_at(result, 50) == pytest.approx(0.25, abs=0.02)
    assert sample_at(result, 500) == pytest.approx(0.5, abs=1e-3)
    assert sample_at(result, 950) == pytest.approx(0.25, abs=0.02)
    assert out_of(result).samples[-1] < 0.002


def test_fades_are_clamped_to_half_the_span() -> None:
    result = mix_audio(
        silence(1000),
        [track(pcm=dc(1000, 0.5), gain_db=0, fade_in_ms=5000, fade_out_ms=5000)],
        FLAT,
    )

    assert sample_at(result, 500) == pytest.approx(
        0.5, abs=0.01
    )  # the two fades meet where both are full
    assert sample_at(result, 250) < 0.5


# Ducking


def ducking_voice() -> PcmAudio:
    values = [0.0] * (2000 * RATE // 1000) + [0.02] * (2000 * RATE // 1000)
    values += [0.0] * (2000 * RATE // 1000)
    return pcm_of(values)


def test_music_is_ducked_under_the_voice_and_recovers() -> None:
    music = track(MixRole.MUSIC, dc(6000, 0.5), gain_db=0)

    result = mix_audio(ducking_voice(), [music], FLAT)

    assert sample_at(result, 1000) == pytest.approx(0.5, abs=0.005)
    # 12 dB down is x0.2512, plus the voice itself.
    assert sample_at(result, 3000) == pytest.approx(0.5 * 0.2512 + 0.02, abs=0.01)
    assert sample_at(result, 5900) == pytest.approx(0.5, abs=0.02)
    assert 0.3 < result.report.ducked_ratio < 0.5


def test_ducking_starts_before_the_voice_and_releases_slowly() -> None:
    result = mix_audio(
        ducking_voice(), [track(MixRole.MUSIC, dc(6000, 0.5), gain_db=0)], FLAT
    )

    assert sample_at(result, 1990) < 0.48  # look-ahead of the attack time
    assert sample_at(result, 1900) > 0.49  # not earlier than the look-ahead
    assert 0.12 < sample_at(result, 4050) < 0.3  # still down shortly after the voice
    assert sample_at(result, 4050) < sample_at(result, 4800) < 0.5


def test_ducking_depth_follows_the_constraint() -> None:
    constraints = MixConstraints(max_boost_db=0, duck_db=6)

    result = mix_audio(
        ducking_voice(), [track(MixRole.MUSIC, dc(6000, 0.5), gain_db=0)], constraints
    )

    assert sample_at(result, 3000) == pytest.approx(
        0.5 * 10 ** (-6 / 20) + 0.02, abs=0.01
    )


def test_music_is_not_ducked_in_silence() -> None:
    result = mix_audio(
        silence(2000), [track(MixRole.MUSIC, dc(2000, 0.5), gain_db=0)], FLAT
    )

    assert sample_at(result, 1000) == pytest.approx(0.5, abs=1e-3)
    assert result.report.ducked_ratio == 0.0


def test_sound_effects_are_not_ducked() -> None:
    result = mix_audio(
        ducking_voice(), [track(MixRole.SFX, dc(6000, 0.5), gain_db=0)], FLAT
    )

    assert sample_at(result, 3000) == pytest.approx(0.52, abs=0.005)
    assert result.report.ducked_ratio == 0.0


# Level and limiter


def test_the_mix_is_brought_to_the_target_rms() -> None:
    result = mix_audio(tone(2000, amp=0.1), [])

    report = result.report
    assert report.pre_gain_rms_dbfs == pytest.approx(-23.0, abs=0.1)
    assert report.normalisation_gain_db == pytest.approx(5.0, abs=0.1)
    assert report.rms_dbfs == pytest.approx(-18.0, abs=0.1)


def test_the_boost_is_capped() -> None:
    result = mix_audio(tone(2000, amp=0.01), [])

    assert result.report.normalisation_gain_db == 12.0
    assert result.report.rms_dbfs < -30


def test_a_loud_voice_is_lowered_only_up_to_the_cap() -> None:
    result = mix_audio(tone(2000, amp=0.9, freq=100), [])

    # the RMS is -3.9 dBFS, 14 dB over the target, but the cap is 12 dB
    assert result.report.normalisation_gain_db == -12.0


def test_a_silent_mix_keeps_its_level() -> None:
    result = mix_audio(silence(1000), [])

    assert result.report.normalisation_gain_db == 0.0
    assert result.report.pre_gain_rms_dbfs == -120.0
    assert result.report.peak_dbfs == -120.0


def test_the_limiter_keeps_the_peak_under_the_ceiling() -> None:
    result = mix_audio(
        silence(1000),
        [track(pcm=dc(1000, 0.9), gain_db=0), track(pcm=dc(1000, 0.9), gain_db=0)],
        FLAT,
    )

    ceiling = 10 ** (DEFAULT_CONSTRAINTS.peak_ceiling_dbfs / 20)
    peak = max(abs(v) for v in out_of(result).samples)
    assert peak <= ceiling + 1 / FULL
    assert result.report.limiter_max_reduction_db > 5
    assert result.report.peak_dbfs <= DEFAULT_CONSTRAINTS.peak_ceiling_dbfs + 0.01


def test_the_limiter_leaves_a_quiet_mix_alone() -> None:
    result = mix_audio(silence(1000), [track(pcm=dc(1000, 0.3), gain_db=0)], FLAT)

    assert result.report.limiter_max_reduction_db == 0.0
    assert sample_at(result, 500) == pytest.approx(0.3, abs=1e-3)


def test_the_limiter_works_on_stereo_peaks() -> None:
    result = mix_audio(
        silence(1000, ch=2), [track(pcm=dc(1000, 1.5, ch=2), gain_db=0)], FLAT
    )

    ceiling = 10 ** (DEFAULT_CONSTRAINTS.peak_ceiling_dbfs / 20)
    assert max(abs(v) for v in out_of(result).samples) <= ceiling + 1 / FULL


# Determinism and limits


def test_the_same_inputs_give_the_same_bytes() -> None:
    def run() -> bytes:
        return mix_audio(
            tone(3000, amp=0.2),
            [
                track(MixRole.MUSIC, tone(1500, freq=440, rate=22050), fade_in_ms=200),
                track(MixRole.SFX, tone(300, freq=900), start_ms=1000),
            ],
        ).wav

    assert run() == run()


def test_a_report_has_plain_numbers_for_the_audit() -> None:
    result = mix_audio(tone(2000), [track(label="a")])

    metadata = result.report.metadata()
    assert metadata["track_count"] == 1 and metadata["warning_count"] == 0
    assert all(isinstance(value, int | float | str) for value in metadata.values())
    assert set(DEFAULT_CONSTRAINTS.to_dict()) >= {"duck_db", "target_rms_dbfs"}


def test_too_many_tracks_or_too_much_audio_is_refused() -> None:
    with pytest.raises(MixInputError):
        mix_audio(
            silence(1000),
            [track(label=f"t{n}") for n in range(3)],
            MixConstraints(max_tracks=2),
        )
    with pytest.raises(MixInputError):
        mix_audio(silence(2000), [], MixConstraints(max_total_seconds=1))


def test_inputs_must_have_the_right_types() -> None:
    with pytest.raises(MixInputError):
        mix_audio("voice", [])  # type: ignore[arg-type]
    with pytest.raises(MixInputError):
        mix_audio(silence(1000), ["track"])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "overrides",
    [
        {"duck_db": -1},
        {"duck_attack_ms": -1},
        {"duck_release_ms": 1.5},
        {"target_rms_dbfs": 3},
        {"peak_ceiling_dbfs": 1},
        {"max_boost_db": float("nan")},
        {"max_tracks": True},
        {"limiter_release_ms": 0},
        {"music_gain_db": 20},
    ],
)
def test_constraints_are_validated(overrides) -> None:
    with pytest.raises(MixInputError):
        MixConstraints(**overrides)
