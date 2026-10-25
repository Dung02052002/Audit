# Animation pipeline (ANIM-01)

A development tool that turns a JSON storyboard into a short vertical film
(1080x1920, 30 fps, 20-30 s) using only free tools. It lives in
`tools/animation_film.py` with the code in `src/ai_youtube_agent/animation/`.
It is not wired into `ScriptToVideoService`.

## What is real and what is not

| Part | How it is made |
| --- | --- |
| Backgrounds | A local ComfyUI server, Stable Diffusion 1.5 (`v1-5-pruned-emaonly.safetensors`): a two-pass hires-fix, txt2img 576x1024 then img2img 1080x1920 at denoise 0.5. |
| Motion | Software 2D animation drawn in code with Pillow (cut-out puppets with inverse kinematics, a virtual camera, drawn props and steam). It is **not an AI video model**. |
| Voice | Microsoft Edge TTS (`vi-VN-HoaiMyNeural` for the narrator, `vi-VN-NamMinhNeural` for the customer). An online free service, **not local**: the tool needs internet for new lines (results are cached). |
| Music, ambience, effects | Synthesized in the tool, in pure Python: no samples, no third-party tracks, no stock audio. Rights-clear. |
| Subtitles | Pillow burn-in from the Edge word-boundary timings (`word_boundary`; a line without boundaries is marked `sentence_estimate`), plus SRT and VTT files. |
| Cost | 0. The paid-API budget guard (BUDGET-01, `free_only`) must report `free` for ComfyUI and Edge TTS before anything runs. |

There is never a placeholder: if ComfyUI is down or the GPU runs out of memory on
every tier, the background step fails and says so.

## Run

```sh
uv run --extra tts --extra demo python tools/animation_film.py \
    --storyboard tools/storyboards/quan_an_nho_shorts.json --contact-sheet
```

ComfyUI is started by hand (default `http://127.0.0.1:8188`). Options:

| Option | Meaning |
| --- | --- |
| `--out-root` | Output root, default `artifacts/animation_film`; the run goes to `<out-root>/<timestamp>/` and an existing run directory is never reused. |
| `--cache-dir` | Cache of Edge results and ComfyUI plates (default `<out-root>/_cache`). A plate is reused only when its prompt, seed, checkpoint and graph parameters are unchanged. |
| `--checkpoint` | ComfyUI checkpoint, default `v1-5-pruned-emaonly.safetensors` (it must be installed in ComfyUI). |
| `--stills 0.5,1.5` | Extra stills at these seconds (start, middle and end are always written). |
| `--stills-only` | Stop after the stills: no audio, video or validation. The fast loop for looking at the picture. |
| `--contact-sheet` | One image with a thumbnail every 1.5 s. |
| `--steps a,b` | Run only these steps (the manifest is always written); a step whose inputs were not produced in the same run is BLOCKED. |
| `--strict` | Every validation check fails the step (default: only the container and decode checks do; the others are warnings in `validation.json`). |

Steps: `budget_check`, `storyboard`, `voice`, `backgrounds`,
`timeline_subtitles`, `stills`, `audio`, `render`, `validate`, `manifest`. A failed
step blocks the later ones; the manifest is written anyway and is the run report.

Output of a full run: the MP4, `*.srt`, `*.vtt`, the stems `narration.wav`,
`music.wav`, `ambience.wav` (both ducked as mixed), `sfx.wav` and the mastered
`mix.wav`, the stills and the contact sheet, `validation.json`, `audio_qc.json`,
`manifest.json`, `timeline.json` and `run.log`.

## Storyboard

`tools/storyboards/quan_an_nho_shorts.json` describes locations (prompt and seed),
characters, and five scenes with their cast, props, spoken lines, timed actions,
shots (framing, camera moves) and transitions. Actions and effects are anchored to
a scene time, a word of a spoken line or the end of another action, so they follow
the real voice. Changing a location prompt or seed invalidates its cached plate
(about five minutes of GPU time per plate).

## The sound

* Music: 84 bpm in the storyboard key (C major). A diatonic progression
  I vi IV V, ending IV V then a closing I(add9), is played by a detuned two-channel
  pad, a soft bass and plucked arpeggios. The scene mood (`sparse`, `tender`,
  `curious`, `busy_warm`, `resolved`) selects the pattern and the density; the last
  chord is rolled and decays naturally under the final fade.
* Ambience per scene (street at dawn, kitchen hum, bubbling, sizzle, stove glow,
  soft chatter), cross-faded at the scene boundaries.
* Effects at the times of the resolved timeline: footsteps from the real foot
  contacts of the walk cycle, clink, bowl, chair, cloth, whoosh, steam hiss and the
  door bell.
* Duck: the speech windows are measured on the real narration track (plus the timed
  words); music is lowered 9 dB and ambience 5 dB under speech (attack 70 ms,
  release 300 ms).
* Master: stems mixed, a limiter, then two-pass `loudnorm` to -14 LUFS and
  -1.5 dBTP.

## Validation

`validate` runs on the final MP4 and writes `validation.json` and `audio_qc.json`:

* Container: H.264 High, 1080x1920, 30 fps, frame count equal to the timeline,
  AAC 48 kHz stereo, duration 20-30 s (never above 60 s), audio and video within
  100 ms, `moov` before `mdat`; a full decode must print no error.
* Motion (mean luma difference of consecutive frames at 108x192): no frozen stretch
  longer than 0.5 s and at least 70% of the seconds of each scene moving. The
  film grain gives this metric a floor of about 0.52, so "frozen" means below 0.62
  and a moving second needs a mean of at least 0.8; both were set from a measured
  run.
* Scenes: adjacent scenes must differ (colour-histogram distance of their middle
  frames).
* Composition: no face under the caption in 98% of the frames, the caption inside
  the safe area (top 12%, bottom 20%, right 12% are kept free), in-world text under
  4% of the frame and characters at least 5x its area.
* Audio: -14 +/- 1.5 LUFS, true peak at most -1 dBTP, LRA at most 11 LU, no clipped
  sample, no digital silence over 400 ms, the bed at least 14 dB below the voice
  during speech and back up by at least 6 dB in pauses of 300 ms or more.

## Known limits

* The characters are cut-out puppets: they act and lip-sync (mouth from the real
  loudness and word timings) but are not hand-drawn animation.
* The final MP4 is large (about 17 Mb/s at CRF 18) because the grain is kept.
* Edge TTS is online and occasionally answers "no audio"; the tool retries each line
  three times.
