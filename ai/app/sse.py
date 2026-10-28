"""SSE 블록 직렬화. 형식은 프론트 파서(lib/api/client.ts streamChat)와 Spring 릴레이가 그대로 읽는
`event: <name>\\ndata: <json>\\n\\n` 이다. 이벤트 이름·순서는 개발문서 04 §4.3 이 정본이다."""
import json
from typing import Any

EVENT_ORDER = ["stage", "intent", "policy", "chain", "token", "citations", "done", "error"]


def sse(event: str, data: Any) -> bytes:
    if event not in EVENT_ORDER:
        raise ValueError(f"unknown SSE event: {event}")
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
