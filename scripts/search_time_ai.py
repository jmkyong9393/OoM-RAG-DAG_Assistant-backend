"""탐색 시간 실측 — AI 쪽. 과제 10건을 어시스턴트에 차례로 묻고 건마다 시작 · 첫 글자 · 종료 시각을 기록한다.

사용: python scripts/search_time_ai.py --user OOM0003 --api https://oomassist.kro.kr/api/v1 [--out eval/results/search_time]
  `--api` 를 생략하면 로컬(`localhost:8080`)로 간다.
  비밀번호는 실행하는 사람이 프롬프트에 직접 입력한다(로컬 리허설은 SEARCH_TIME_PASSWORD 환경변수도 된다). 화면 · 파일에 찍지 않는다.
  계정은 과제의 세 부서(SW 개발 · 경영지원 · 고객지원)에 모두 속해 있어야 한다.
  질문은 화면에서 물은 것과 똑같이 그 계정의 질문 기록으로 서버에 남고, 부서 대시보드의 질문 수 · 응답 시간 · 근거 없이 끝난 질문 비율 · 많이 다시 쓰인 사례에도 들어간다(한 번 실행에 과제 수만큼).

결과: <out>/AI_<사번>_<실행 시각>.csv — 건마다 시작 · 첫 글자 · 종료 시각과 걸린 초, 서버가 잰 값, 답변 원문. 정답 칸은 취합하는 사람이 채운다.
  시간은 요청을 보낸 때부터 재므로 네트워크 왕복이 들어 있다. 사람이 질문을 입력하고 답을 읽는 시간은 들어 있지 않다.
  과제와 정답은 data/eval/search_time_tasks_v1.json.
"""
import argparse, csv, getpass, http.client, http.cookiejar, json, os, pathlib, statistics, sys, time, urllib.error, urllib.request
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
TASKS = ROOT / "data" / "eval" / "search_time_tasks_v1.json"
KIND = {"policy": "규정", "troubleshoot": "사례", "both": "규정+사례"}
COLUMNS = ["과제", "종류", "부서", "시작", "첫 글자", "종료", "첫 글자까지(초)", "걸린 시간(초)", "서버 첫 글자(ms)", "서버 전체(ms)",
           "분류", "확인된 근거 수", "오류", "질문", "답변", "정답(취합 때 O/X)"]


class _PlainHttpPolicy(http.cookiejar.DefaultCookiePolicy):
    """로컬(http://localhost)에서도 Secure 가 붙은 로그인 쿠키를 다시 실어 보낸다 — load_logs.py 와 같다."""
    def return_ok_secure(self, cookie, request):
        return True


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def error_code(e: urllib.error.HTTPError) -> str:
    """오류 응답 본문의 code(FORBIDDEN · CONSENT_REQUIRED 등). 읽지 못하면 빈 문자열."""
    try:
        return str(json.loads(e.read()).get("code", ""))
    except (ValueError, OSError, AttributeError):
        return ""


def ask(op, api: str, ws: int, query: str) -> dict:
    """질문 하나 → 시각과 답변. 시작은 요청을 보내기 직전, 첫 글자는 첫 token 이벤트, 종료는 done 이벤트."""
    req = urllib.request.Request(api + "/chat", method="POST", data=json.dumps({"query": query}).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream", "X-Workspace-Id": str(ws)})
    r = {"started": now(), "first": "", "ended": "", "first_sec": "", "sec": "", "text": "", "verified": "", "done": {}, "error": ""}
    t0, event = time.monotonic(), ""
    try:
        with op.open(req, timeout=180) as resp:
            for raw in resp:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:])
                    if event == "token":
                        if not r["first"] and data["text"].strip():
                            r["first"], r["first_sec"] = now(), round(time.monotonic() - t0, 1)
                        r["text"] += data["text"]
                    elif event == "citations":
                        r["verified"] = len(data["verified"])
                    elif event == "error":
                        r["error"] = data["message"]
                    elif event == "done":
                        r["ended"], r["sec"], r["done"] = now(), round(time.monotonic() - t0, 1), data
                        break
    except urllib.error.HTTPError as e:
        r["error"] = f"HTTP {e.code} {error_code(e)}".strip()
    except (OSError, http.client.HTTPException) as e:   # 연결 끊김 · 시간 초과 — 그 건만 오류로 적고 다음 건으로 간다
        r["error"] = f"연결 오류: {type(e).__name__}"
    if not r["ended"] and not r["error"]:
        r["error"] = "done 없이 끝남"
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", required=True, help="질문을 보낼 계정의 사번(질문 기록이 이 계정에 남는다)")
    ap.add_argument("--api", default="http://localhost:8080/api/v1")
    ap.add_argument("--out", default=str(ROOT / "eval" / "results" / "search_time"))
    a = ap.parse_args()
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar(policy=_PlainHttpPolicy())))

    pw = os.environ.get("SEARCH_TIME_PASSWORD") or getpass.getpass(f"{a.user} 비밀번호: ")
    login = urllib.request.Request(a.api + "/auth/login", method="POST", headers={"Content-Type": "application/json"},
                                   data=json.dumps({"employee_no": a.user, "password": pw}).encode())
    del pw
    try:
        op.open(login, timeout=30).close()
    except urllib.error.HTTPError as e:
        print(f"로그인 실패 {e.code}")
        return 1
    except OSError as e:
        print(f"서버에 연결하지 못했다: {a.api} ({type(e).__name__})")
        return 1

    print(f"{a.api} · {a.user} · 과제 {len(tasks)}건 — 이 서버에 질문 {len(tasks)}건이 기록된다")
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"AI_{a.user}_{datetime.now():%Y%m%d-%H%M%S}.csv"
    rows = []
    f = open(path, "w", encoding="utf-8-sig", newline="")   # utf-8-sig — Excel 이 한글을 바로 읽는다
    w = csv.writer(f)
    w.writerow(COLUMNS)
    for n, t in enumerate(tasks, 1):
        r = ask(op, a.api, t["workspace_id"], t["task"])
        lat = r["done"].get("latency", {})
        rows.append([t["id"], KIND[t["kind"]], t["workspace"], r["started"], r["first"], r["ended"], r["first_sec"], r["sec"],
                     lat.get("first_token_ms", ""), lat.get("total_ms", ""), r["done"].get("intent", ""), r["verified"], r["error"], t["task"], r["text"], ""])
        print(f"[{n}/{len(tasks)}] 과제 {t['id']} · {t['workspace']} — 시작 {r['started'][11:19]} · 첫 글자 {r['first_sec']}초 · 종료 {r['ended'][11:19]} · {r['sec']}초"
              + (f" · 오류: {r['error']}" if r["error"] else ""))
        w.writerow(rows[-1])
        f.flush()   # 건마다 바로 적는다 — 도중에 끊겨도 앞 건은 남는다
    f.close()
    try:
        op.open(urllib.request.Request(a.api + "/auth/logout", method="POST"), timeout=30).close()
    except OSError:   # 로그아웃이 실패해도 기록은 이미 파일에 있다
        pass

    secs = [row[7] for row in rows if row[7] != ""]
    if secs:
        print(f"걸린 시간 중간값 {statistics.median(secs)}초 · 합 {round(sum(secs), 1)}초 ({len(secs)}/{len(tasks)}건)")
    print(f"기록: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
