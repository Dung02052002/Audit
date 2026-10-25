"""Software 2D animation pipeline (ANIM-01).

Turns a storyboard (JSON) into a short vertical film made only with free tools:
backgrounds from a local ComfyUI server, Vietnamese speech from Edge TTS with
word timings, everything else (characters, camera, subtitles, music, sound
effects) computed and drawn by code. It is not an AI video model and never
returns a placeholder in place of a failed step.

The modules that only compute (``storyboard``, ``timeline``, ``camera``,
``easing``, the pure part of ``subtitles``, ``tts``, ``backgrounds``,
``manifest``) never import Pillow at module level, so they work without the
``demo`` extra; Pillow is imported lazily inside the functions that draw.
"""
