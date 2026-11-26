"""사례 대응표({"items": [...]})를 POST /logs/bulk 로 넣는다 — 도메인 규칙은 서버가 같은 트랜잭션에서 검사한다(하나라도 어기면 전부 취소).

사용:  python scripts/bulk_load.py --file notebook_cases.json --workspace 1 --api https://oomassist.kro.kr/api/v1 --user OOM0001
       비밀번호는 실행하는 사람이 프롬프트에 직접 입력한다(로컬 리허설은 BULK_PASSWORD 환경변수도 된다). 화면 · 로그에 찍지 않는다.
       --check 는 서버에 보내지 않고 건수 · 장애 건만 보여 준다.
같은 source_ref 가 이미 있으면 서버가 'existing' 으로 건너뛴다 — 다시 돌려도 두 번 들어가지 않는다.
절차(백업 · 스냅샷 · 되돌리기)는 개발문서 48_사례_일괄_적재와_되돌리기.md.
"""
from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", required=True)
    ap.add_argument("--workspace", type=int, required=True)
    ap.add_argument("--api", default="http://localhost:8080/api/v1")
    ap.add_argument("--user", required=True, help="작성자로 남을 계정의 사번")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    with open(a.file, encoding="utf-8") as f:
        items = json.load(f)["items"]
    roots = [i for i in items if not i.get("parent_ref") and not i.get("parent_refs")]
    print(f"기록 {len(items)}건 · 장애 건 {len(roots)}건 → 부서 {a.workspace} · {a.api} · 작성자 {a.user}")
    for r in roots:
        print(f"  - {r.get('source_ref', '')}  {r['title']}")
    if a.check:
        return 0
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(method, path, body=None, headers=None):
        req = urllib.request.Request(a.api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with op.open(req, timeout=180) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:500]

    pw = os.environ.get("BULK_PASSWORD") or getpass.getpass(f"{a.user} 비밀번호: ")
    s, _ = call("POST", "/auth/login", {"employee_no": a.user, "password": pw})
    del pw
    if s != 200:
        print(f"로그인 실패 {s}")
        return 1
    s, body = call("POST", "/logs/bulk", {"items": items}, {"X-Workspace-Id": str(a.workspace)})
    if s not in (200, 201):
        print(f"일괄 등록 실패 {s}: {body}")
        return 1
    ids = [i["id"] for i in body["items"]]
    print(f"일괄 등록 {s} — 생성 {body['created']} · 이미 있음 {body['existing']} · id {min(ids)} ~ {max(ids)}")
    call("POST", "/auth/logout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
