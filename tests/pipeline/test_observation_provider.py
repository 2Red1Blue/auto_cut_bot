from __future__ import annotations

import json

from auto_cut_bot.pipeline.vlm.observation_factory import build_observation_ark_body


def test_observation_ark_wire_is_clean_and_has_one_video_input() -> None:
    private_id = "source-receipt-should-never-be-on-wire"
    body = build_observation_ark_body(
        model_id="model-observation",
        file_id="provider-file-1",
        prompt="只观察当前视频。",
        response_schema={"type": "object", "properties": {"description": {"type": "string"}}},
        max_output_tokens=2048,
    )
    encoded = json.dumps(body, ensure_ascii=False)
    assert private_id not in encoded
    assert body["input"] == [{"role": "user", "content": [
        {"type": "input_video", "file_id": "provider-file-1"},
        {"type": "input_text", "text": "只观察当前视频。"},
    ]}]
    assert body["stream"] is True and body["store"] is True
    assert body["thinking"] == {"type": "disabled"}
    assert "context_pack" not in encoded and "semantic" not in encoded
