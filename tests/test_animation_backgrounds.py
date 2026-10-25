"""Animation backgrounds: two-pass graph, VRAM ladder A -> B -> C -> fail, cache."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import pytest

from ai_youtube_agent.animation.backgrounds import (
    POST_RESIZE_LABEL,
    BackgroundTier,
    TwoPassPlan,
    build_two_pass_graph,
    generate_backgrounds,
    is_out_of_memory,
    plan_for_tier,
    png_is_blank,
    validate_plate,
)
from ai_youtube_agent.animation.errors import AnimationError
from ai_youtube_agent.providers.registry.comfyui_adapter import (
    ComfyUIAdapter,
    HttpResponse,
)
from animation_fixtures import make_plate_png, shipped_storyboard
from media_fixtures import make_jpeg_header

CKPT = "v1-5-pruned-emaonly.safetensors"
OOM = "Allocation on device 0 would exceed allowed memory. CUDA out of memory."


def json_response(payload: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode("utf-8"))


@dataclass
class FakeComfy:
    """Scripted ComfyUI: one outcome per submitted workflow (the last repeats).

    Outcomes: ``ok``, ``oom`` (execution error), ``other`` (execution error that
    is not memory), ``reject`` (HTTP 400), ``blank`` (a flat PNG),
    ``small`` (a PNG of the wrong size).
    """

    outcomes: list[str] = field(default_factory=lambda: ["ok"])
    graphs: list[dict[str, Any]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    free_calls: int = 0
    _kinds: dict[str, str] = field(default_factory=dict)

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        path = urlsplit(url).path
        self.paths.append(f"{method} {path}")
        if path.startswith("/object_info/"):
            return json_response(
                {
                    "CheckpointLoaderSimple": {
                        "input": {"required": {"ckpt_name": [[CKPT]]}}
                    }
                }
            )
        if path == "/prompt":
            return self._submit(body or b"{}")
        if path.startswith("/history/"):
            return self._history(path.rsplit("/", 1)[1])
        if path == "/view":
            return self._view()
        if path == "/free":
            self.free_calls += 1
            return HttpResponse(200, b"")
        return HttpResponse(404, b"{}")

    def _submit(self, body: bytes) -> HttpResponse:
        graph = json.loads(body)["prompt"]
        self.graphs.append(graph)
        count = len(self.graphs)
        kind = self.outcomes[min(count - 1, len(self.outcomes) - 1)]
        if kind == "reject":
            return json_response({"error": {"message": "bad workflow"}}, 400)
        prompt_id = f"p{count}"
        self._kinds[prompt_id] = kind
        return json_response(
            {"prompt_id": prompt_id, "number": count, "node_errors": {}}
        )

    def _history(self, prompt_id: str) -> HttpResponse:
        kind = self._kinds[prompt_id]
        if kind in ("oom", "other"):
            message = (
                OOM if kind == "oom" else "Cannot execute because a node is missing"
            )
            status = {
                "status_str": "error",
                "completed": False,
                "messages": [
                    [
                        "execution_error",
                        {"node_type": "VAEEncode", "exception_message": message},
                    ]
                ],
            }
            return json_response({prompt_id: {"status": status, "outputs": {}}})
        entry = {
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {
                "9": {
                    "images": [
                        {
                            "filename": f"{prompt_id}.png",
                            "subfolder": "",
                            "type": "output",
                        }
                    ]
                }
            },
        }
        return json_response({prompt_id: entry})

    def _view(self) -> HttpResponse:
        kind = self._kinds[f"p{len(self.graphs)}"]
        if kind == "blank":
            return HttpResponse(200, make_plate_png(1080, 1920, blank=True))
        if kind == "small":
            return HttpResponse(200, make_plate_png(640, 960))
        return HttpResponse(200, make_plate_png(1080, 1920))


def adapter_for(fake: FakeComfy) -> ComfyUIAdapter:
    return ComfyUIAdapter(
        default_model=CKPT,
        transport=fake,
        sleep=lambda _s: None,
        poll_interval_seconds=0.01,
        job_timeout_seconds=900.0,
    )


def run(fake: FakeComfy, tmp_path, **kwargs: Any):
    return generate_backgrounds(
        adapter_for(fake),
        shipped_storyboard(),
        tmp_path / "out",
        tmp_path / "cache",
        log=lambda _m: None,
        **kwargs,
    )


def class_types(graph: Mapping[str, Any]) -> dict[str, str]:
    return {node_id: node["class_type"] for node_id, node in graph.items()}


# --- the graph -----------------------------------------------------------------


def build(tier: BackgroundTier, **kwargs: Any) -> dict[str, Any]:
    plan = plan_for_tier(tier)
    return build_two_pass_graph(
        plan,
        kwargs.get("positive", "a plate"),
        kwargs.get("negative", "no people"),
        1234,
        CKPT,
        tiled=tier is not BackgroundTier.A,
    )


def test_tier_a_graph_is_the_two_pass_hires_fix() -> None:
    graph = build(BackgroundTier.A, positive="a plate, flat style")

    assert class_types(graph) == {
        "4": "CheckpointLoaderSimple",
        "6": "CLIPTextEncode",
        "7": "CLIPTextEncode",
        "5": "EmptyLatentImage",
        "3": "KSampler",
        "8": "VAEDecode",
        "10": "ImageScale",
        "11": "VAEEncode",
        "12": "KSampler",
        "13": "VAEDecode",
        "9": "SaveImage",
    }
    assert graph["4"]["inputs"]["ckpt_name"] == CKPT
    assert graph["6"]["inputs"]["text"] == "a plate, flat style"
    assert graph["7"]["inputs"]["text"] == "no people"
    assert graph["5"]["inputs"] == {"batch_size": 1, "height": 1024, "width": 576}
    first, second = graph["3"]["inputs"], graph["12"]["inputs"]
    assert (first["steps"], first["cfg"], first["denoise"]) == (24, 7.0, 1.0)
    assert (second["steps"], second["denoise"]) == (20, 0.5)
    assert first["sampler_name"] == second["sampler_name"] == "euler_ancestral"
    assert first["seed"] == second["seed"] == 1234
    assert second["latent_image"] == ["11", 0]
    scale = graph["10"]["inputs"]
    assert (scale["upscale_method"], scale["width"], scale["height"]) == (
        "lanczos",
        1080,
        1920,
    )
    assert graph["9"]["inputs"]["images"] == ["13", 0]


def test_tier_b_uses_explicit_tiled_vae_nodes() -> None:
    graph = build(BackgroundTier.B)

    types = class_types(graph)
    assert types["11"] == "VAEEncodeTiled" and types["13"] == "VAEDecodeTiled"
    for node in ("11", "13"):
        inputs = graph[node]["inputs"]
        assert inputs["tile_size"] == 512 and inputs["overlap"] == 64
        assert {"temporal_size", "temporal_overlap"} <= set(inputs)
    assert graph["10"]["inputs"]["width"] == 1080
    assert "14" not in graph


def test_tier_c_renders_810x1440_and_resizes_with_lanczos() -> None:
    graph = build(BackgroundTier.C)

    assert class_types(graph)["11"] == "VAEEncodeTiled"
    assert (graph["10"]["inputs"]["width"], graph["10"]["inputs"]["height"]) == (
        810,
        1440,
    )
    resize = graph["14"]["inputs"]
    assert (resize["upscale_method"], resize["width"], resize["height"]) == (
        "lanczos",
        1080,
        1920,
    )
    assert resize["image"] == ["13", 0]
    assert graph["9"]["inputs"]["images"] == ["14", 0]
    assert plan_for_tier(BackgroundTier.C).post_resize == POST_RESIZE_LABEL
    assert plan_for_tier(BackgroundTier.A).post_resize is None


def test_graph_is_json_serialisable_and_deterministic() -> None:
    first = build(BackgroundTier.B)

    assert json.loads(json.dumps(first)) == first == build(BackgroundTier.B)


# --- generation and the ladder --------------------------------------------------


def test_tier_a_is_used_when_the_card_has_enough_memory(tmp_path) -> None:
    fake = FakeComfy()

    records = run(fake, tmp_path)

    assert set(records) == {"front", "kitchen"}
    front = records["front"]
    assert (front.tier, front.tiled_vae, front.post_resize) == ("A", False, None)
    assert (front.pass1_size, front.pass2_size, front.final_size) == (
        (576, 1024),
        (1080, 1920),
        (1080, 1920),
    )
    assert front.cache_hit is False
    assert front.seed == 20261101 and records["kitchen"].seed == 20261102
    assert front.prompt_id == "p1" and records["kitchen"].prompt_id == "p2"
    assert "two-pass hires-fix" in front.method
    assert (tmp_path / "out" / "front.png").read_bytes() == make_plate_png(1080, 1920)
    assert len(fake.graphs) == 2 and fake.free_calls == 0
    positive = fake.graphs[0]["6"]["inputs"]["text"]
    assert positive.startswith(
        "eye-level straight-on view of one small Vietnamese street eatery"
    )
    assert positive.endswith("vertical composition")
    assert "person, people" in fake.graphs[0]["7"]["inputs"]["text"]
    assert fake.graphs[0]["3"]["inputs"]["seed"] == 20261101


def test_oom_on_a_falls_back_to_b(tmp_path) -> None:
    fake = FakeComfy(["oom", "ok", "ok"])
    freed: list[int] = []

    records = run(fake, tmp_path, free_vram=lambda: freed.append(1))

    assert records["front"].tier == "B"
    assert records["front"].tiled_vae is True and records["front"].post_resize is None
    assert [class_types(g)["11"] for g in fake.graphs[:2]] == [
        "VAEEncode",
        "VAEEncodeTiled",
    ]
    assert freed == [1]
    assert records["kitchen"].tier == "A"  # each location starts from tier A again


def test_oom_on_a_and_b_ends_on_c_with_the_post_resize_label(tmp_path) -> None:
    fake = FakeComfy(["oom", "oom", "ok", "ok", "ok"])

    records = run(fake, tmp_path)

    front = records["front"]
    assert front.tier == "C"
    assert (front.pass2_size, front.final_size) == ((810, 1440), (1080, 1920))
    assert front.tiled_vae is True
    assert front.post_resize == "lanczos 1.333x, not a diffusion pass"
    assert "post_resize: " not in front.method and "lanczos 1.333x" in front.method
    tiers = [class_types(g) for g in fake.graphs[:3]]
    assert [t["11"] for t in tiers] == ["VAEEncode", "VAEEncodeTiled", "VAEEncodeTiled"]
    assert ["14" in t for t in tiers] == [False, False, True]
    assert fake.free_calls == 2  # POST /free between attempts
    assert (tmp_path / "out" / "front.png").exists()


def test_oom_on_every_tier_fails_and_makes_no_placeholder(tmp_path) -> None:
    fake = FakeComfy(["oom"])

    with pytest.raises(AnimationError, match="every tier.*no placeholder"):
        run(fake, tmp_path)

    assert len(fake.graphs) == 3
    assert not list((tmp_path / "out").glob("*.png"))
    assert not list((tmp_path / "cache").glob("*.png"))


def test_an_error_that_is_not_memory_does_not_walk_down_the_ladder(tmp_path) -> None:
    fake = FakeComfy(["other"])

    with pytest.raises(
        AnimationError, match="ComfyUI failed for background 'front' on tier A"
    ):
        run(fake, tmp_path)

    assert len(fake.graphs) == 1 and fake.free_calls == 0


def test_a_rejected_workflow_is_not_retried(tmp_path) -> None:
    fake = FakeComfy(["reject"])

    with pytest.raises(AnimationError, match="rejected the workflow"):
        run(fake, tmp_path)

    assert len(fake.graphs) == 1


def test_a_blank_plate_is_an_error_not_a_result(tmp_path) -> None:
    with pytest.raises(AnimationError, match="blank"):
        run(FakeComfy(["blank"]), tmp_path)

    assert not list((tmp_path / "out").glob("*.png"))


def test_a_plate_of_the_wrong_size_is_an_error(tmp_path) -> None:
    with pytest.raises(AnimationError, match="ComfyUI failed"):
        run(FakeComfy(["small"]), tmp_path)


def test_plates_are_cached_and_reused(tmp_path) -> None:
    first = FakeComfy()
    run(first, tmp_path)
    second = FakeComfy()

    records = run(second, tmp_path)

    assert second.graphs == []
    assert all(r.cache_hit for r in records.values())
    assert records["front"].prompt_id == "p1"
    assert records["front"].tier == "A"
    assert len(list((tmp_path / "cache").glob("*.png"))) == 2


def test_the_cache_remembers_the_tier_that_made_the_plate(tmp_path) -> None:
    run(FakeComfy(["oom", "oom", "ok", "ok", "ok"]), tmp_path)

    records = run(FakeComfy(), tmp_path)

    assert records["front"].tier == "C" and records["front"].cache_hit
    assert records["front"].post_resize == POST_RESIZE_LABEL


def test_a_damaged_cache_entry_is_generated_again(tmp_path) -> None:
    run(FakeComfy(), tmp_path)
    for png in (tmp_path / "cache").glob("*.png"):
        png.write_bytes(b"\x89PNG broken")
    fake = FakeComfy()

    run(fake, tmp_path)

    assert len(fake.graphs) == 2


def test_changing_the_seed_misses_the_cache(tmp_path) -> None:
    run(FakeComfy(), tmp_path)
    storyboard = shipped_storyboard()
    other = TwoPassPlan(pass1_steps=30)
    fake = FakeComfy()

    generate_backgrounds(
        adapter_for(fake),
        storyboard,
        tmp_path / "out",
        tmp_path / "cache",
        base_plan=other,
        log=lambda _m: None,
    )

    assert len(fake.graphs) == 2
    assert fake.graphs[0]["3"]["inputs"]["steps"] == 30


def test_record_serialises_for_the_manifest(tmp_path) -> None:
    record = run(FakeComfy(), tmp_path)["front"]

    data = record.as_dict()

    assert json.loads(json.dumps(data)) == data
    assert data["pass1_size"] == [576, 1024]
    assert data["sha256"] and data["path"].endswith("front.png")


# --- checks --------------------------------------------------------------------


def test_validate_plate_accepts_a_real_plate_only() -> None:
    validate_plate(make_plate_png(1080, 1920), (1080, 1920))
    with pytest.raises(AnimationError, match="expected 1080x1920"):
        validate_plate(make_plate_png(640, 960), (1080, 1920))
    with pytest.raises(AnimationError, match="blank"):
        validate_plate(make_plate_png(1080, 1920, blank=True), (1080, 1920))
    with pytest.raises(AnimationError):
        validate_plate(b"not an image", (1080, 1920))
    with pytest.raises(AnimationError, match="not a PNG"):
        validate_plate(make_jpeg_header(1080, 1920), (1080, 1920))


def test_blank_detection() -> None:
    assert png_is_blank(make_plate_png(640, 960, blank=True))
    assert not png_is_blank(make_plate_png(640, 960))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("CUDA out of memory. Tried to allocate 2.00 GiB", True),
        ("Allocation on device", True),
        ("torch.OutOfMemoryError: OOM when allocating", True),
        ("Cannot execute because a node is missing", False),
        ("the zoom room is closed", False),
    ],
)
def test_out_of_memory_detection(message: str, expected: bool) -> None:
    assert is_out_of_memory(RuntimeError(message)) is expected
