"""Animation sound: synthesis, ducking, the mix command and the audio QC (no network).

The synthesis is pure Python, so most tests render a few seconds and check real
properties of the samples (pitch, decay, level, determinism). The two ffmpeg
tests run the real mastering chain on short tones and are skipped without ffmpeg.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import wave
from array import array
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_youtube_agent.animation import audio_mix, audio_qc
from ai_youtube_agent.animation import audio_synth as synth
from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.animation.timeline import ResolvedLine, SfxEvent, resolve_timeline
from animation_fixtures import fake_spoken, shipped_storyboard

RATE = synth.RATE
C_MAJOR_PCS = {0, 2, 4, 5, 7, 9, 11}
SCENES = [
    (0.0, 4.0, "sparse"),
    (4.0, 10.0, "tender"),
    (10.0, 16.0, "curious"),
    (16.0, 21.0, "busy_warm"),
    (21.0, 28.0, "resolved"),
]
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


def goertzel(samples, freq: float, rate: int = RATE) -> float:
    """Power of one frequency in ``samples`` (Goertzel)."""
    k = 2 * math.cos(2 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for value in samples:
        s1, s2 = value + k * s1 - s2, s1
    return s1 * s1 + s2 * s2 - k * s1 * s2


# --- music: key, chords, plan ------------------------------------------------


def test_parse_key_reads_major_and_minor_keys() -> None:
    assert synth.parse_key("C major")[0:2] == (0, synth.MAJOR_SCALE)
    assert synth.parse_key("A minor")[0:2] == (9, synth.MINOR_SCALE)
    assert synth.parse_key("F# major")[0] == 6
    assert synth.parse_key("Bb")[0] == 10
    with pytest.raises(AnimationError, match="music key"):
        synth.parse_key("H lydian")


def test_progression_ends_subdominant_dominant_without_repeating_a_chord() -> None:
    for bars in range(3, 14):
        degrees = synth.progression_degrees(bars)
        assert len(degrees) == bars
        assert degrees[-2:] == [synth.SUBDOMINANT, synth.DOMINANT]
        assert all(a != b for a, b in zip(degrees, degrees[1:], strict=False))
    assert synth.progression_degrees(1) == [0]
    assert synth.progression_degrees(2) == [0, synth.DOMINANT]


def test_the_closing_chord_is_the_tonic_with_a_ninth_on_the_beat_grid() -> None:
    tonic, scale, _ = synth.parse_key("C major")
    chords = synth.plan_chords(28.285, 84, tonic, scale)

    closing = chords[-1]
    beat = 60 / 84
    assert closing.final and closing.root == 0
    assert set(closing.tones) == {0, 4, 7, 11, 2}  # C E G B D
    assert closing.start_s / beat == pytest.approx(round(closing.start_s / beat))
    assert 2.5 <= closing.duration_s <= 3.6
    assert chords[-2].degree == synth.DOMINANT
    # contiguous bars, first on I
    assert chords[0].start_s == 0 and chords[0].degree == 0
    for a, b in zip(chords, chords[1:], strict=False):
        assert a.end_s == pytest.approx(b.start_s)


def test_every_planned_note_is_in_the_key() -> None:
    events = synth.plan_music(28.285, 84, "C major", SCENES)

    assert events
    for event in events:
        if event.kind in ("pluck", "bass", "pad"):
            assert event.midi % 12 in C_MAJOR_PCS, event
        assert event.start_s >= 0 and event.dur_s > 0
        assert event.start_s < 28.285


def test_pad_voicings_have_no_semitone_clusters() -> None:
    tonic, scale, _ = synth.parse_key("C major")
    for chord in synth.plan_chords(28.285, 84, tonic, scale):
        notes = synth.pad_notes(chord)
        assert 3 <= len(notes) <= 4
        gaps = [b - a for a, b in zip(notes, notes[1:], strict=False)]
        assert all(g >= 3 for g in gaps), (chord, notes)
        assert all(synth.PAD_LOW <= n <= synth.PAD_HIGH for n in notes)


def test_moods_change_the_texture() -> None:
    def kinds(mood: str) -> dict[str, int]:
        events = synth.plan_music(20.0, 84, "C major", [(0.0, 20.0, mood)])
        counts: dict[str, int] = {}
        for event in events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        return counts

    sparse, tender, busy = kinds("sparse"), kinds("tender"), kinds("busy_warm")
    assert sparse["pluck"] < tender["pluck"]
    assert busy.get("shaker", 0) > 0 and "shaker" not in sparse
    assert busy["bass"] > sparse["bass"]


def test_the_plan_is_deterministic_and_the_seed_changes_the_humanising() -> None:
    a = synth.plan_music(12.0, 84, "C major", SCENES[:2], seed=7)
    b = synth.plan_music(12.0, 84, "C major", SCENES[:2], seed=7)
    c = synth.plan_music(12.0, 84, "C major", SCENES[:2], seed=8)

    assert a == b
    assert sorted(e.midi for e in a) == sorted(e.midi for e in c)  # same notes
    assert [e.start_s for e in a] != [e.start_s for e in c]  # other timing jitter


# --- voices and rendering -------------------------------------------------------


def test_a_pluck_is_in_tune_and_decays() -> None:
    tone = synth.pluck_tone(440.0, 1.5, tau_s=0.8)

    head = tone[: RATE // 5]
    assert goertzel(head, 440.0) > 50 * goertzel(head, 466.16)  # a semitone away
    early = synth.rms_of(tone[: RATE // 10])
    late = synth.rms_of(tone[-RATE // 4 :])
    assert late < 0.3 * early
    assert abs(tone[0]) < 0.01 and abs(tone[-1]) < 0.01  # no clicks at the ends


def test_a_pad_tone_has_its_fundamental_and_a_steady_level() -> None:
    tone = synth.pad_tone(196.0, 2.0, 0.5)

    assert goertzel(tone, 196.5) > 20 * goertzel(tone, 233.0)
    first, second = synth.rms_of(tone[:RATE]), synth.rms_of(tone[RATE:])
    assert second == pytest.approx(first, rel=0.2)


def test_one_pole_filters_split_low_and_high_content() -> None:
    low = [math.sin(2 * math.pi * 200 * i / RATE) for i in range(RATE // 4)]
    high = [math.sin(2 * math.pi * 8000 * i / RATE) for i in range(RATE // 4)]

    assert synth.rms_of(synth.lowpass(low, 1000)) > 0.6 * synth.rms_of(low)
    assert synth.rms_of(synth.lowpass(high, 500)) < 0.1 * synth.rms_of(high)
    assert synth.rms_of(synth.highpass(low, 4000)) < 0.1 * synth.rms_of(low)
    assert synth.rms_of(synth.highpass(high, 1000)) > 0.8 * synth.rms_of(high)


def test_rendered_music_is_stereo_quiet_enough_and_ends_on_a_decay() -> None:
    total = 6.0
    events = synth.plan_music(total, 84, "C major", [(0.0, total, "tender")])
    left, right = synth.render_music(events, total)

    assert len(left) == len(right) == int(math.ceil(total * RATE))
    peak = max(synth.peak_of(left), synth.peak_of(right))
    assert peak == pytest.approx(synth.PEAK, rel=0.01)  # -6 dBFS before the mix
    assert all(math.isfinite(v) for v in left[:: RATE // 10])
    assert left != right  # panned notes and detuned pads give width
    middle = synth.rms_of(left[2 * RATE : 3 * RATE])
    tail = synth.rms_of(left[-RATE // 4 :])
    assert 0 < tail < 1.5 * middle  # the closing chord rings out, no cut
    assert abs(left[-1]) < 0.02


def test_music_is_deterministic() -> None:
    events = synth.plan_music(4.0, 84, "C major", [(0.0, 4.0, "curious")])

    first = synth.render_music(events, 4.0)
    second = synth.render_music(events, 4.0)

    assert first[0] == second[0] and first[1] == second[1]


@pytest.mark.parametrize("kind", sorted(synth.AMBIENCE_GAIN_DB))
def test_every_ambience_kind_renders_a_bed_at_the_reference_level(kind: str) -> None:
    layer = synth.render_ambience_layer(kind, 0.8, seed=11)

    assert len(layer) == int(0.8 * RATE)
    assert synth.rms_of(layer) == pytest.approx(0.1, rel=0.2)
    assert all(math.isfinite(v) for v in layer[:: RATE // 20])
    assert layer == synth.render_ambience_layer(kind, 0.8, seed=11)
    assert layer != synth.render_ambience_layer(kind, 0.8, seed=12)


def test_unknown_ambience_and_effects_are_refused() -> None:
    with pytest.raises(AnimationError, match="unknown ambience"):
        synth.render_ambience_layer("jungle", 1.0, 1)
    with pytest.raises(AnimationError, match="unknown sound effect"):
        synth.render_sfx("laser")
    with pytest.raises(AnimationError, match="unknown sound effect"):
        synth.render_sfx_track([SfxEvent("laser", 100, "x")], 1.0)


def test_ambience_scenes_crossfade_and_cover_the_whole_film() -> None:
    bed = synth.render_ambience(
        [(0.0, 1.0, ["kitchen_hum"]), (1.0, 2.0, ["street"])], 2.0, seed=3
    )

    assert len(bed) == 2 * RATE
    assert synth.peak_of(bed) == pytest.approx(synth.PEAK, rel=0.01)
    for start in range(0, 2 * RATE, RATE // 4):  # never a silent gap
        assert synth.rms_of(bed[start : start + RATE // 4]) > 0.005


@pytest.mark.parametrize("kind", synth.SFX_KINDS)
def test_every_effect_is_a_short_normalised_sound(kind: str) -> None:
    sound = synth.render_sfx(kind)

    assert 0.05 * RATE < len(sound) < 2 * RATE
    assert synth.peak_of(sound) == pytest.approx(1.0)
    assert abs(sound[0]) < 0.05 and abs(sound[-1]) < 0.05
    assert synth.render_sfx(kind, 1) != synth.render_sfx(kind, 0) or kind in (
        "door_bell",
        "steam_hiss",
    )


def test_effects_have_the_character_of_their_name() -> None:
    footstep, clink = synth.render_sfx("footstep"), synth.render_sfx("clink")

    assert goertzel(footstep, 80) > goertzel(footstep, 2300)  # a low thud
    assert goertzel(clink, 2300) > 10 * goertzel(clink, 80)  # a high ring
    bell = synth.render_sfx("door_bell")
    assert goertzel(bell, 2093) > goertzel(bell, 700)


def test_the_effect_track_places_sounds_at_their_times() -> None:
    events = [SfxEvent("clink", 1000, "a"), SfxEvent("bowl_place", 2500, "b")]

    track = synth.render_sfx_track(events, 4.0)

    assert len(track) == 4 * RATE
    assert synth.rms_of(track[: RATE - 100]) == 0.0
    assert synth.rms_of(track[RATE : RATE + RATE // 10]) > 0.01
    assert synth.rms_of(track[int(1.8 * RATE) : int(2.4 * RATE)]) == 0.0
    assert synth.rms_of(track[int(2.5 * RATE) : int(2.6 * RATE)]) > 0.01


# --- narration, speech windows, ducking ---------------------------------------------


def write_tone_wav(path: Path, seconds: float, rate: int = 24000, amp: float = 0.3):
    count = int(seconds * rate)
    frames = struct.pack(
        f"<{count}h",
        *[
            round(amp * 32767 * math.sin(2 * math.pi * 220 * i / rate))
            for i in range(count)
        ],
    )
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(frames)
    return path


def resolved_line(line_id: str, wav: Path, start_ms: int, end_ms: int) -> ResolvedLine:
    return ResolvedLine(
        line_id=line_id,
        scene_id="s",
        voice="narrator",
        text="x",
        start_ms=start_ms,
        end_ms=end_ms,
        pause_after_ms=0,
        words=(),
        timing_source="word_boundary",
        wav_path=wav,
    )


def test_narration_is_placed_on_the_film_clock_at_one_level(tmp_path: Path) -> None:
    quiet = write_tone_wav(tmp_path / "a.wav", 0.5, amp=0.1)
    loud = write_tone_wav(tmp_path / "b.wav", 0.5, amp=0.6)
    lines = [
        resolved_line("a", quiet, 500, 1000),
        resolved_line("b", loud, 2000, 2500),
    ]

    track = audio_mix.place_narration(lines, 3000)

    assert len(track) == 3 * audio_mix.NARRATION_RATE
    rate = audio_mix.NARRATION_RATE
    assert synth.rms_of(track[: rate // 2 - 50]) == 0.0
    first = synth.rms_of(track[int(0.55 * rate) : int(0.95 * rate)])
    second = synth.rms_of(track[int(2.05 * rate) : int(2.45 * rate)])
    assert first == pytest.approx(audio_mix.VOICE_RMS, rel=0.05)
    assert second == pytest.approx(first, rel=0.05)  # both lines normalised


def test_narration_rejects_a_wav_at_the_wrong_rate(tmp_path: Path) -> None:
    wav = write_tone_wav(tmp_path / "x.wav", 0.2, rate=16000)

    with pytest.raises(AnimationError, match="16000 Hz"):
        audio_mix.place_narration([resolved_line("x", wav, 0, 200)], 1000)


def narration_with_bursts(bursts: list[tuple[int, int]], total_ms: int = 4000) -> array:
    rate = audio_mix.NARRATION_RATE
    track = synth.zeros(total_ms * rate // 1000)
    for start, end in bursts:
        for i in range(start * rate // 1000, end * rate // 1000):
            track[i] = 0.2 * math.sin(2 * math.pi * 200 * i / rate)
    return track


def test_speech_windows_merge_short_gaps_and_drop_blips() -> None:
    track = narration_with_bursts(
        [(500, 1000), (1100, 1500), (2500, 3000), (3500, 3520)]
    )

    windows = audio_mix.speech_windows(track, audio_mix.NARRATION_RATE)

    assert len(windows) == 2  # the 100 ms gap is bridged, the 20 ms blip is dropped
    (a0, a1), (b0, b1) = windows
    assert a0 == pytest.approx(500, abs=20) and a1 == pytest.approx(1500, abs=20)
    assert b0 == pytest.approx(2500, abs=20) and b1 == pytest.approx(3000, abs=20)


def test_speech_windows_include_the_timed_words_even_when_they_are_quiet() -> None:
    track = narration_with_bursts([(500, 1000)])

    windows = audio_mix.speech_windows(
        track, audio_mix.NARRATION_RATE, [(1000, 1300), (2000, 2400)]
    )

    assert windows[0][0] == pytest.approx(500, abs=20)
    assert windows[0][1] == 1300  # extended by the word that follows closely
    assert windows[1] == (2000, 2400)


def test_duck_envelope_is_unity_in_pauses_and_down_by_the_depth_in_speech() -> None:
    gains = audio_mix.duck_gain_envelope(
        [(1000.0, 2000.0)], 4000.0, depth_db=9.0, attack_ms=100.0, release_ms=400.0
    )

    def at(ms: float) -> float:
        return gains[int(ms * RATE / 1000)]

    assert at(100) == pytest.approx(1.0)
    assert at(3500) == pytest.approx(1.0)
    assert at(1500) == pytest.approx(10 ** (-9 / 20), rel=1e-3)
    assert at(1000) == pytest.approx(10 ** (-9 / 20), rel=0.02)  # down at the 1st word
    assert 10 ** (-9 / 20) < at(950) < 1.0  # falling during the attack
    assert 10 ** (-9 / 20) < at(2200) < 1.0  # recovering during the release
    assert at(2500) == pytest.approx(1.0, abs=1e-3)
    falling = [at(900 + i * 10) for i in range(10)]
    assert falling == sorted(falling, reverse=True)
    assert len(gains) >= 4 * RATE


def test_overlapping_windows_never_duck_deeper_than_the_depth() -> None:
    gains = audio_mix.duck_gain_envelope([(500.0, 1500.0), (1400.0, 2200.0)], 3000.0)

    assert min(gains) == pytest.approx(10 ** (-9 / 20), rel=1e-3)


def test_apply_gain_multiplies_sample_by_sample() -> None:
    out = audio_mix.apply_gain(array("d", [1.0, 2.0, 3.0]), array("d", [0.5, 0.5, 1.0]))

    assert list(out) == [0.5, 1.0, 3.0]


# --- WAV I/O and the ffmpeg command ---------------------------------------------------


def test_wav_roundtrip_and_clipping(tmp_path: Path) -> None:
    path = audio_mix.write_wav(
        tmp_path / "x.wav", [[0.0, 0.5, -0.5, 2.0], [0.1, 0.2, 0.3, -2.0]], 48000
    )

    channels, rate = audio_qc.read_pcm16(path)

    assert rate == 48000 and len(channels) == 2
    assert list(channels[0]) == [0, 16384, -16384, 32767]
    assert list(channels[1])[-1] == -32768  # clipped, not wrapped
    mono, mono_rate = audio_mix.read_wav_float(path)
    assert mono_rate == 48000 and mono[1] == pytest.approx(0.5)


def test_mix_command_has_no_file_path_in_the_filter_and_masters_to_target() -> None:
    paths = [Path(f"{n}.wav") for n in ("n", "m", "a", "s")]

    analysis = audio_mix.build_mix_command(*paths, None, total_s=28.3, fade_out_ms=500)
    final = audio_mix.build_mix_command(
        *paths,
        Path("mix.wav"),
        total_s=28.3,
        fade_out_ms=500,
        measured={
            "input_i": "-18.2",
            "input_tp": "-3.7",
            "input_lra": "3.2",
            "input_thresh": "-28.4",
            "target_offset": "0.1",
        },
    )

    graph = analysis[analysis.index("-filter_complex") + 1]
    assert "amix=inputs=4:normalize=0" in graph
    assert "afade=t=out:st=27.800:d=0.500" in graph
    assert "alimiter=limit=0.89" in graph and "level=0" in graph
    assert graph.endswith("apad=whole_dur=28.300[mix]")  # the end is padded back
    assert "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json" in graph
    assert analysis[-3:] == ["-f", "null", "-"]
    final_graph = final[final.index("-filter_complex") + 1]
    assert "measured_I=-18.2" in final_graph and "linear=true" in final_graph
    assert final[-1] == "mix.wav" and "pcm_s16le" in final
    assert not any(":\\" in part or ".wav:" in graph for part in analysis)


def test_loudnorm_json_is_read_from_the_end_of_ffmpeg_output() -> None:
    stderr = (
        "size=N/A time=00:00:28.28 bitrate=N/A\n"
        "[Parsed_loudnorm_5 @ 0000012] \n{\n"
        '\t"input_i" : "-18.23",\n\t"input_tp" : "-3.80",\n\t"input_lra" : "3.30",\n'
        '\t"input_thresh" : "-28.54",\n\t"output_i" : "-14.38",\n'
        '\t"target_offset" : "0.38"\n}\n'
    )

    data = audio_mix.parse_loudnorm_json(stderr)

    assert data["input_i"] == "-18.23" and data["target_offset"] == "0.38"
    with pytest.raises(AnimationError, match="no measurement"):
        audio_mix.parse_loudnorm_json("nothing useful here")


class FakeFfmpeg:
    """Stands in for ``subprocess.run`` in the two-pass master."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        stderr = ""
        if command[-3:] == ["-f", "null", "-"]:
            stderr = (
                '{\n"input_i" : "-18.0",\n"input_tp" : "-3.0",\n"input_lra" : "3.0",\n'
                '"input_thresh" : "-28.0",\n"target_offset" : "0.0"\n}'
            )
        elif self.returncode == 0:
            Path(command[-1]).write_bytes(b"RIFF")
        return SimpleNamespace(returncode=self.returncode, stderr=stderr, stdout=b"")


@pytest.fixture
def quick_synth(monkeypatch):
    """Replace the slow synthesis with short buffers: only the wiring is tested."""

    def music(events, seconds, **_kw):
        count = int(seconds * RATE)
        tone = array("d", [0.3 * math.sin(i / 20) for i in range(count)])
        return tone, tone

    monkeypatch.setattr(synth, "render_music", music)
    monkeypatch.setattr(
        synth,
        "render_ambience",
        lambda scenes, seconds, **_kw: array("d", [0.1] * int(seconds * RATE)),
    )


def test_build_audio_writes_the_stems_and_runs_the_two_pass_master(
    tmp_path: Path, quick_synth
) -> None:
    storyboard = shipped_storyboard()
    spoken = fake_spoken(storyboard)
    for line in list(spoken.values()):
        write_tone_wav(tmp_path / f"{line.line_id}.wav", line.duration_ms / 1000)
        spoken[line.line_id] = replace(line, wav_path=tmp_path / f"{line.line_id}.wav")
    timeline = resolve_timeline(storyboard, spoken)
    fake = FakeFfmpeg()
    logs: list[str] = []

    build = audio_mix.build_audio(
        timeline, tmp_path / "out", run=fake, log=logs.append, footsteps=[]
    )

    assert [
        p.name for p in (build.narration, build.music, build.ambience, build.sfx)
    ] == [
        "narration.wav",
        "music.wav",
        "ambience.wav",
        "sfx.wav",
    ]
    assert all(p.exists() for p in (build.narration, build.music, build.mix))
    assert len(fake.commands) == 2
    assert fake.commands[0][-3:] == ["-f", "null", "-"]
    assert (
        "measured_I=-18.0"
        in fake.commands[1][fake.commands[1].index("-filter_complex") + 1]
    )
    assert build.windows and build.total_ms >= timeline.total_ms
    assert build.loudnorm_pass1["input_i"] == "-18.0"
    channels, rate = audio_qc.read_pcm16(build.music)
    assert rate == RATE and len(channels) == 2
    assert len(channels[0]) == int(math.ceil(build.total_ms * RATE / 1000))
    assert any("speech windows" in line for line in logs)
    assert build.as_dict()["mix"].endswith("mix.wav")


def test_build_audio_reports_an_ffmpeg_failure(tmp_path: Path, quick_synth) -> None:
    storyboard = shipped_storyboard()
    spoken = fake_spoken(storyboard)
    for line in list(spoken.values()):
        write_tone_wav(tmp_path / f"{line.line_id}.wav", line.duration_ms / 1000)
        spoken[line.line_id] = replace(line, wav_path=tmp_path / f"{line.line_id}.wav")
    timeline = resolve_timeline(storyboard, spoken)

    with pytest.raises(AnimationError, match="loudness analysis failed"):
        audio_mix.build_audio(
            timeline,
            tmp_path / "out",
            run=FakeFfmpeg(returncode=1),
            log=lambda _m: None,
        )


@needs_ffmpeg
def test_the_real_master_reaches_the_target_loudness(tmp_path: Path) -> None:
    seconds = 8.0
    voice = audio_mix.write_wav(
        tmp_path / "n.wav",
        [
            [
                0.1 * math.sin(2 * math.pi * 220 * i / 24000)
                for i in range(int(seconds * 24000))
            ]
        ],
        24000,
    )
    tone = [
        0.03 * math.sin(2 * math.pi * 330 * i / RATE)
        for i in range(int(seconds * RATE))
    ]
    music = audio_mix.write_wav(tmp_path / "m.wav", [tone, tone], RATE)
    amb = audio_mix.write_wav(tmp_path / "a.wav", [tone, tone], RATE)
    sfx = audio_mix.write_wav(tmp_path / "s.wav", [[0.0] * int(seconds * RATE)], RATE)
    first = audio_mix.build_mix_command(
        voice, music, amb, sfx, None, total_s=seconds, fade_out_ms=500
    )
    proc = subprocess.run(first, capture_output=True, text=True, check=True)
    measured = audio_mix.parse_loudnorm_json(proc.stderr)
    out = tmp_path / "mix.wav"
    final = audio_mix.build_mix_command(
        voice, music, amb, sfx, out, total_s=seconds, fade_out_ms=500, measured=measured
    )
    subprocess.run(final, capture_output=True, text=True, check=True)

    report = audio_qc.measure_loudness(out, "ffmpeg")

    assert report.integrated_lufs == pytest.approx(-14.0, abs=1.0)
    assert report.true_peak_dbtp <= -1.0
    channels, rate = audio_qc.read_pcm16(out)
    assert rate == 48000 and len(channels[0]) == pytest.approx(seconds * 48000, abs=50)


# --- audio QC -----------------------------------------------------------------------

EBUR128_SUMMARY = """
[Parsed_ebur128_0 @ 000001] Summary:

  Integrated loudness:
    I:         -14.1 LUFS
    Threshold: -24.2 LUFS

  Loudness range:
    LRA:         3.1 LU
    Threshold: -34.2 LUFS
    LRA low:   -17.0 LUFS
    LRA high:  -13.9 LUFS

  True peak:
    Peak:       -1.5 dBFS
"""


def test_parse_ebur128_reads_the_summary_not_the_running_lines() -> None:
    noisy = (
        "[Parsed_ebur128_0] t: 1.0 M: -20 S: -21 I: -99.0 LUFS LRA: 0.0 LU\n"
        + EBUR128_SUMMARY
    )

    report = audio_qc.parse_ebur128(noisy)

    assert report == audio_qc.LoudnessReport(-14.1, -1.5, 3.1)
    with pytest.raises(AnimationError, match="no loudness summary"):
        audio_qc.parse_ebur128("garbage")


def test_measure_loudness_uses_ebur128_with_true_peak() -> None:
    seen: list[list[str]] = []

    def run(command, **_kw):
        seen.append(command)
        return SimpleNamespace(returncode=0, stderr=EBUR128_SUMMARY)

    report = audio_qc.measure_loudness(Path("x.mp4"), "ffmpeg", run=run)

    assert report.integrated_lufs == -14.1
    assert "ebur128=peak=true" in seen[0] and "-vn" in seen[0]
    with pytest.raises(AnimationError, match="ebur128 failed"):
        audio_qc.measure_loudness(
            Path("x.mp4"),
            "ffmpeg",
            run=lambda *_a, **_k: SimpleNamespace(returncode=1, stderr="x"),
        )


def test_sample_level_measurements() -> None:
    assert audio_qc.peak_dbfs([0, 16384, -8000]) == pytest.approx(-6.02, abs=0.01)
    assert audio_qc.peak_dbfs([0, 0]) == -math.inf
    assert audio_qc.clipped_samples([0, 32767, -32768, 100]) == 2
    levels = audio_qc.windowed_rms_db([10000] * 480 + [0] * 480, 48000, 10)
    assert len(levels) == 2
    assert levels[0] == pytest.approx(-10.3, abs=0.1) and levels[1] == -math.inf
    quiet = [0] * 2400 + [5000] * 100 + [0] * 600
    assert audio_qc.longest_silence_ms(quiet, 48000) == pytest.approx(50.0)


def test_speech_gaps_include_the_head_and_the_tail_and_skip_short_ones() -> None:
    windows = [(400.0, 1000.0), (1200.0, 2000.0), (2800.0, 3500.0)]

    gaps = audio_qc.speech_gaps(windows, 4500.0, min_gap_ms=300)

    assert gaps == [(0.0, 400.0), (2000.0, 2800.0), (3500.0, 4500.0)]


def stems(depth_db: float, gap_ms: int = 800) -> tuple[list[float], list[float], list]:
    """Voice and a bed: 1 s speech, a ``gap_ms`` pause, 1 s speech (ducked)."""
    rate = 8000
    windows = [(0.0, 1000.0), (1000.0 + gap_ms, 2000.0 + gap_ms)]
    total = int((2000 + gap_ms) * rate / 1000)
    voice = [0.0] * total
    bed = [0.0] * total
    for index in range(total):
        t = index * 1000 / rate
        speaking = any(a <= t < b for a, b in windows)
        voice[index] = 10000 * math.sin(index / 7) if speaking else 0.0
        level = 10 ** (-depth_db / 20) if speaking else 1.0
        bed[index] = 1000 * level * math.sin(index / 11)
    return voice, bed, windows


def test_bed_below_voice_and_duck_depth_are_measured_from_the_stems() -> None:
    voice, bed, windows = stems(9.0)

    below = audio_qc.bed_vs_voice_db(voice, 8000, bed, 8000, windows)
    depth, gaps = audio_qc.duck_depth_db(bed, 8000, windows, 2800.0)

    assert below == pytest.approx(20 + 9, abs=0.3)  # 10000 vs 1000 is 20 dB, plus 9
    assert depth == pytest.approx(9.0, abs=0.5)
    assert gaps == 1


def test_qc_raises_when_there_is_nothing_to_measure() -> None:
    with pytest.raises(AnimationError, match="no speech window"):
        audio_qc.bed_vs_voice_db([0.0] * 800, 8000, [0.0] * 800, 8000, [])
    voice, bed, windows = stems(9.0, gap_ms=100)
    with pytest.raises(AnimationError, match="no gap long enough"):
        audio_qc.duck_depth_db(bed, 8000, windows[:1] + [(1050.0, 2100.0)], 2100.0)


def test_the_verdict_lists_every_threshold_that_fails() -> None:
    result = audio_qc.AudioQcResult()
    audio_qc.evaluate_loudness(audio_qc.LoudnessReport(-11.0, 0.5, 14.0), result)
    audio_qc.evaluate_samples([[0, 32767, 0] * 100 + [0] * 30000], 48000, result)
    voice, bed, windows = stems(2.0)
    audio_qc.evaluate_mix_balance(voice, 8000, bed, 8000, windows, 2800.0, result)

    problems = " | ".join(result.problems)
    assert not result.passed
    for fragment in (
        "integrated loudness -11.0",
        "true peak 0.5",
        "loudness range 14.0",
        "clipped samples",
        "digital silence",
        "the bed comes back only",  # a 2 dB duck is too shallow
    ):
        assert fragment in problems
    assert "below the voice" not in problems  # 22 dB below the voice is fine


def test_a_clean_result_passes_and_serialises() -> None:
    result = audio_qc.AudioQcResult()
    audio_qc.evaluate_loudness(audio_qc.LoudnessReport(-14.0, -1.5, 3.0), result)
    audio_qc.evaluate_samples([[0, 1000, -1000] * 1000], 48000, result)
    voice, bed, windows = stems(9.0)
    audio_qc.evaluate_mix_balance(voice, 8000, bed, 8000, windows, 2800.0, result)

    data = result.as_dict()

    assert result.passed and data["passed"] and data["problems"] == []
    assert data["loudness"]["integrated_lufs"] == -14.0
    assert data["thresholds"]["target_lufs"] == -14.0
    assert data["duck_depth_db"] == pytest.approx(9.0, abs=0.5)
    assert data["clipped_samples"] == 0


def test_the_audio_modules_work_without_pillow() -> None:
    code = (
        "import sys; sys.modules['PIL'] = None\n"
        "from ai_youtube_agent.animation import audio_synth, audio_mix\n"
        "from ai_youtube_agent.animation import audio_qc, validate\n"
        "assert not any(name.startswith('PIL.') for name in sys.modules)\n"
        "print('ok')\n"
    )

    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
