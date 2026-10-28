"""OpenAI 호환 API 스텁 (테스트 전용, 키 발급 전 openai 코드 경로 검증).
/chat/completions — JSON 모드: system 프롬프트로 분류/정제를 구분해 그럴듯한 JSON 을 돌려준다.
                    stream=true: 컨텍스트에 등장한 마커 중 앞 2개 + 없는 마커 [N99] 를 인용하는 문장을 delta 로 흘리고 usage 청크를 보낸다.
/embeddings       — 텍스트 해시 기반의 결정적 1536차원 단위 벡터.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 1536
MARKER_RE = re.compile(r"\[([PN]\d+)\]")


def fake_embedding(text: str) -> list[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vals = [((h[i % 32] * 31 + i * 7) % 211) / 211.0 - 0.5 for i in range(DIM)]
    n = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [v / n for v in vals]


class Handler(BaseHTTPRequestHandler):
    server_version = "openai-stub/0.1"
    calls: list[dict] = []

    def log_message(self, *a):  # 조용히
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        auth = self.headers.get("Authorization", "")
        Handler.calls.append({"path": self.path, "auth": auth, "body": req})
        if not auth.startswith("Bearer "):
            return self._json(401, {"error": {"message": "missing bearer"}})
        if self.path.endswith("/embeddings"):
            inputs = req["input"] if isinstance(req["input"], list) else [req["input"]]
            return self._json(200, {"object": "list", "model": req.get("model"),
                                    "data": [{"index": i, "embedding": fake_embedding(t)} for i, t in enumerate(inputs)],
                                    "usage": {"prompt_tokens": 3 * len(inputs), "total_tokens": 3 * len(inputs)}})
        if self.path.endswith("/chat/completions"):
            system = next((m["content"] for m in req["messages"] if m["role"] == "system"), "")
            user = next((m["content"] for m in req["messages"] if m["role"] == "user"), "")
            if req.get("stream"):
                return self._stream(req, user)
            if "classify" in system:
                intent = "both" if ("규정" in user and ("오류" in user or "에러" in user)) else "policy" if "규정" in user else "troubleshoot"
                content = json.dumps({"intent": intent, "reason": "stub"})
            elif "Extract" in system:
                content = json.dumps({"error_code": None, "keywords": ["스텁", "키워드", "정제"], "resolution_summary": "스텁이 정리한 해결 요약."})
            else:
                content = json.dumps({})
            return self._json(200, {"id": "stub", "object": "chat.completion", "model": req.get("model"),
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49}})
        return self._json(404, {"error": {"message": "unknown path"}})

    def _stream(self, req, user: str) -> None:
        markers = list(dict.fromkeys(MARKER_RE.findall(user)))
        cite = " ".join(f"[{m}]" for m in markers[:2])
        text = f"스텁 답변입니다. 근거는 {cite} 이고, 검증되지 않은 인용 [N99] 도 하나 섞습니다."
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        pieces = re.findall(r"\S+\s*", text)
        limit = req.get("max_tokens")
        finish = "length" if limit and limit < len(pieces) else "stop"
        for piece in pieces[:limit]:
            chunk = {"id": "stub", "object": "chat.completion.chunk", "model": req.get("model"),
                     "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        end = {"id": "stub", "object": "chat.completion.chunk", "model": req.get("model"),
               "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
        self.wfile.write(f"data: {json.dumps(end)}\n\n".encode("utf-8"))
        if (req.get("stream_options") or {}).get("include_usage"):
            usage = {"id": "stub", "object": "chat.completion.chunk", "choices": [],
                     "usage": {"prompt_tokens": 120, "completion_tokens": 25, "total_tokens": 145}}
            self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class OpenAIStub:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self):
        Handler.calls.clear()
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()

    @property
    def calls(self) -> list[dict]:
        return Handler.calls
