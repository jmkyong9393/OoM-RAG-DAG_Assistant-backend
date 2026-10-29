"""SSE 이벤트 payload 의 JSON Schema 를 Pydantic 원본(ai/app/llm/schemas.py)에서 뽑아 data/contracts/sse_events.schema.json 에 쓴다.
프론트는 이 파일로 타입을 생성한다 (예: npx json-schema-to-typescript). 이벤트 이름·순서는 ai/app/sse.py EVENT_ORDER.

실행: cd ai && uv run python ../scripts/export_sse_schema.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai"))

from app.llm import schemas as S  # noqa: E402
from app.sse import EVENT_ORDER  # noqa: E402

EVENTS = {
    "session": {"type": "object", "properties": {"session_id": {"type": "integer"}}, "required": ["session_id"],
                "description": "Spring 이 첫 이벤트로 송출 (Python 스트림에는 없음)"},
    "stage": S.StageEvent, "intent": S.IntentEvent,
    "policy": {"type": "array", "items": {"$ref": "#/$defs/PolicyRef"}},
    "chain": S.ChainEvent, "token": S.TokenEvent, "citations": S.CitationsEvent, "done": S.DoneEvent, "error": S.ErrorEvent,
}


def main() -> None:
    defs: dict = {}
    props: dict = {}
    for name, model in EVENTS.items():
        if isinstance(model, dict):
            props[name] = model
            continue
        sch = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(sch.pop("$defs", {}))
        defs[model.__name__] = sch
        props[name] = {"$ref": f"#/$defs/{model.__name__}"}
    defs["PolicyRef"] = S.PolicyRef.model_json_schema(ref_template="#/$defs/{model}")
    out = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "OoM Assist SSE events",
        "description": "POST /api/v1/chat 스트림의 event 이름별 data payload. 순서: session → " + " → ".join(EVENT_ORDER),
        "type": "object", "properties": props, "$defs": defs,
        "x-event-order": ["session", *EVENT_ORDER],
    }
    dest = ROOT / "data" / "contracts" / "sse_events.schema.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {dest.relative_to(ROOT)} ({len(defs)} defs)")


if __name__ == "__main__":
    main()
