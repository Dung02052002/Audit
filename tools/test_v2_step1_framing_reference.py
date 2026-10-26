"""Step 1 of Phase G: Generate and verify reference image for framing.

Generates a native 9:16 vertical (384x640) portrait of Mai using V2ShotPreset.CLOSE_UP_PORTRAIT
and verifies that framing captures face, hair, and shoulders without extreme macro eye/skin zoom.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from PIL import Image

PROJECT_ROOT = Path("D:/Project_Audit")
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "v2_upgrade_pipeline" / "step1_framing_reference"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

COMFYUI_URL = "http://127.0.0.1:8188"
BASE_CKPT = "v1-5-pruned-emaonly.safetensors"

# V2 Character Profile for Mai
MAI_TOKENS = (
    "22yo Vietnamese woman Mai, delicate oval face, expressive dark brown eyes, "
    "natural skin texture, long straight black hair with center part, "
    "traditional white silk Ao Dai with high collar"
)

# Safe Portrait Prompt: head and shoulders, chest up, natural framing
PORTRAIT_PROMPT = (
    f"head and shoulders portrait of, chest up, natural facial framing, centered, "
    f"{MAI_TOKENS}, gentle sweet smile, soft morning sunlight, cinematic lighting, photorealistic, 8k, masterpiece"
)

SAFE_NEGATIVE = (
    "extreme close-up, macro skin, skin pores, eye macro, cropped, out of frame, cut off, "
    "deformed face, bad eyes, disfigured, bad teeth, bad anatomy, cross-eyed, mutated hands, "
    "extra limbs, extra fingers, blurry, low quality, worst quality, distorted, static, ugly, cartoon, 3d render"
)

SEED = 823491723
WIDTH = 384
HEIGHT = 640


def build_reference_workflow() -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": BASE_CKPT}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": PORTRAIT_PROMPT}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": SAFE_NEGATIVE}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": SEED,
                "steps": 20,
                "cfg": 6.5,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "v2_ref_portrait_9x16", "images": ["6", 0]}},
    }


def main():
    print("=" * 70)
    print("STEP 1: GENERATING AND VERIFYING REFERENCE FRAMING IMAGE (9:16)")
    print("=" * 70)

    wf = build_reference_workflow()
    payload = json.dumps({"prompt": wf}).encode("utf-8")
    req = urllib.request.Request(f"{COMFYUI_URL}/prompt", data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        p_data = json.loads(resp.read().decode("utf-8"))
    prompt_id = p_data["prompt_id"]

    start = time.time()
    while time.time() - start < 120:
        h_req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
        with urllib.request.urlopen(h_req, timeout=10) as r:
            h = json.loads(r.read().decode("utf-8"))
            if prompt_id in h:
                hist = h[prompt_id]
                break
        time.sleep(1.0)
    duration = time.time() - start
    print(f"Reference generated in {duration:.2f} seconds.")

    images = hist.get("outputs", {}).get("7", {}).get("images", [])
    assert images, "No image generated!"
    img_meta = images[0]

    params = urllib.parse.urlencode({"filename": img_meta["filename"], "subfolder": img_meta.get("subfolder", ""), "type": "output"})
    url = f"{COMFYUI_URL}/view?{params}"
    out_path = OUTPUT_DIR / "ref_portrait_9x16.png"
    urllib.request.urlretrieve(url, out_path)
    print(f"Downloaded reference image to {out_path}")

    # Inspect image dimensions and framing
    with Image.open(out_path) as im:
        w, h = im.size
        print(f"Image dimensions: {w}x{h} (Aspect ratio: {w/h:.3f})")
        assert w == WIDTH and h == HEIGHT, f"Expected {WIDTH}x{HEIGHT}, got {w}x{h}"

    print("Step 1 SUCCESS: Reference image verified.")


if __name__ == "__main__":
    main()
