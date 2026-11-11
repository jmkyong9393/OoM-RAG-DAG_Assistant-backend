#!/usr/bin/env bash
# 운영 설정 파일 deploy/.env.prod 를 만든다(gitignore 대상). 이미 있으면 건드리지 않는다 — DB 비밀번호는 첫 기동 때 볼륨에 굳어서 나중에 바꾸면 접속이 깨진다.
# DB · 시드 비밀번호는 새로 만들고, LLM 설정은 로컬 deploy/.env 에서 옮긴다. 값은 화면에 찍지 않는다.
# 사용: bash deploy/lightsail/make-env.sh <서버 IP> [도메인] [--http]
#   도메인을 안 주면 <IP>.sslip.io (IP 를 가리키는 무료 DNS 이름)로 HTTPS 인증서를 받는다. --http 는 인증서 없이 평문(임시 확인용).
set -euo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8   # Windows 콘솔(cp949)에서 한글 출력 오류 방지
cd "$(dirname "$0")/.."
IP=${1:?서버 IP}; shift
DOMAIN=""; HTTP=0
for a in "$@"; do case "$a" in --http) HTTP=1;; *) DOMAIN=$a;; esac; done
OUT=.env.prod
[ -f "$OUT" ] && { echo "$OUT 이 이미 있습니다 — 그대로 둡니다(다시 만들려면 서버의 DB 볼륨도 함께 지워야 합니다)."; exit 0; }
[ -f .env ] || { echo "deploy/.env 가 없습니다(LLM 키를 옮겨 올 곳)"; exit 1; }
PY=""; for c in python3 python py; do if "$c" -c "import json" >/dev/null 2>&1; then PY=$c; break; fi; done   # Windows 의 python3 은 실행되지 않는 스토어 껍데기일 수 있다
[ -n "$PY" ] || { echo "파이썬이 필요합니다"; exit 1; }

"$PY" - "$IP" "$DOMAIN" "$HTTP" "$OUT" <<'EOF'
import re, secrets, string, sys
ip, domain, http, out = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
host = domain or ip.replace(".", "-") + ".sslip.io"
site, origin, secure = (f"http://{ip}", f"http://{ip}", "false") if http else (host, f"https://{host}", "true")

def seed_password() -> str:
    """작성규칙(대 · 소문자 · 숫자 · 특수문자, 같은 문자 3연속 · 순차 3연속 금지)을 지키는 16자."""
    pool = string.ascii_letters + string.digits
    while True:
        p = "Oom!" + "".join(secrets.choice(pool) for _ in range(10)) + "-" + secrets.choice(string.digits)
        runs = any(a == b == c for a, b, c in zip(p, p[1:], p[2:]))   # PasswordPolicy.hasRun 과 같은 규칙
        seq = any(abs(ord(b) - ord(a)) == 1 and ord(c) - ord(b) == ord(b) - ord(a) for a, b, c in zip(p, p[1:], p[2:]))
        if not runs and not seq and re.search(r"[a-z]", p[4:]) and re.search(r"[A-Z]", p[4:]) and re.search(r"\d", p[4:]):
            return p

local = {}
for line in open(".env", encoding="utf-8"):
    m = re.match(r"\s*([A-Z_]+)=(.*)", line)
    if m:
        local[m.group(1)] = re.sub(r"\s+#.*$", "", m.group(2)).strip()
if not local.get("LLM_PROFILE_DEFAULT_API_KEY"):
    sys.exit("deploy/.env 에 LLM_PROFILE_DEFAULT_API_KEY 가 없습니다")
rows = [
    ("# 운영 설정 — make-env.sh 가 만든 파일. 커밋하지 않는다(.gitignore). 서버에는 deploy/.env 로 올라간다", None),
    ("POSTGRES_DB", "oom"), ("POSTGRES_USER", "oom"),
    ("POSTGRES_PASSWORD", secrets.token_hex(16)), ("OOM_API_PASSWORD", secrets.token_hex(16)), ("OOM_AI_PASSWORD", secrets.token_hex(16)),
    ("# 시드 계정 전원의 초기 비밀번호 — 로그인할 때 이 값을 쓴다", None),
    ("SEED_PASSWORD", seed_password()),
    ("SITE_ADDRESS", site), ("SITE_ORIGIN", origin), ("SESSION_SECURE_COOKIE", secure),
    ("# 로그인 전 대문에 보여 줄 데모 회사(비우면 빈 목록)", None),
    ("PUBLIC_TENANT", "retail"),
    ("AI_MODE", "openai"),
] + [(k, local.get(k, "")) for k in ("LLM_PROFILE_DEFAULT_BASE_URL", "LLM_PROFILE_DEFAULT_API_KEY", "LLM_PROFILE_DEFAULT_MODEL_LIGHT",
                                     "LLM_PROFILE_DEFAULT_MODEL_ANSWER", "LLM_PROFILE_DEFAULT_EMBEDDING_MODEL")] + [
    ("# 이미지 태그는 2-ship.sh 가 올릴 때 채운다", None),
    ("API_IMAGE", ""), ("AI_IMAGE", ""), ("WEB_IMAGE", ""),
]
with open(out, "w", encoding="utf-8", newline="\n") as f:
    for k, v in rows:
        f.write(k + "\n" if v is None else f"{k}={v}\n")
print(f"{out} 을 만들었습니다 — 주소 {origin}")
print("시드 계정 비밀번호는 이 파일의 SEED_PASSWORD 입니다(화면에 찍지 않았습니다).")
EOF
chmod 600 "$OUT" 2>/dev/null || true
