"""사례 적재 스크립트가 운영의 Secure 로그인 쿠키를 평문 http 로도 다시 보내는가(운영 첫 배포에서 401 로 멈춘 것)."""
import http.server
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import load_logs  # noqa: E402


class _Server(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):   # 로그인 — Spring 운영 설정처럼 Secure 쿠키
        self.send_response(200)
        self.send_header("Set-Cookie", "OOMSESSION=abc; Path=/; Secure; HttpOnly; SameSite=Lax")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):    # 로그인 뒤 요청 — 쿠키가 실려 와야 200
        ok = "OOMSESSION=abc" in (self.headers.get("Cookie") or "")
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"cookie": ok}).encode())


def test_secure_session_cookie_is_sent_back_over_plain_http():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        api = load_logs.Api(f"http://127.0.0.1:{srv.server_port}")
        assert api.call("POST", "/auth/login", {})[0] == 200
        assert api.call("GET", "/logs")[0] == 200
    finally:
        srv.shutdown()
