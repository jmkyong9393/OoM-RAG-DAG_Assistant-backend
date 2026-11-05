"""data/logs/*.json 합성 로그를 API 로 적재한다 (D-2).

로그인(세션 쿠키) → 파일마다 X-Workspace-Id → POST /logs/bulk → data/logs/<file>.ids.json(ref·source_ref → id)
→ `_deprecate_reason` 이 있는 노드는 PATCH /logs/{id} {status: deprecated}. 재실행하면 bulk 는 source_ref 로 `existing`,
폐기는 이미 deprecated 면 건너뛴다. 평가용 필드(expected_queries, _refine, _deprecate_reason)는 보내지 않는다.

사용: python scripts/load_logs.py [파일...] [--base URL] [--user 사번]   (파일 생략 시 data/logs/*_v1.json 전부, ORDER 순서. IT 회사 파일은 IT 계정으로)
환경: PORT(기본 8080) · SEED_PASSWORD(기본 Oom!demo-2026) — make load-logs 는 deploy/.env 를 읽는다
"""
import argparse, json, os, sys, pathlib, urllib.request, urllib.error, http.cookiejar

ROOT = pathlib.Path(__file__).resolve().parent.parent
STRIP = ("expected_queries", "_refine", "_deprecate_reason")


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.open = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())).open

    def call(self, method: str, path: str, body=None, ws: int | None = None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if ws is not None:
            req.add_header("X-Workspace-Id", str(ws))
        try:
            with self.open(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"message": raw[:300]}


def load_file(api: Api, path: pathlib.Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    ws, items = doc["workspace_id"], doc["items"]
    payload = [{k: v for k, v in it.items() if k not in STRIP} for it in items]
    status, res = api.call("POST", "/logs/bulk", {"items": payload}, ws)
    if status != 201:
        sys.exit(f"{path.name}: bulk 실패 {status} {res}")
    ids = {r["ref"]: r["id"] for r in res["items"]}
    by_source = {it["source_ref"]: ids[it["ref"]] for it in items}
    out = path.with_suffix(".ids.json")
    out.write_text(json.dumps({"workspace_id": ws, "file": path.name, "ref": ids, "source_ref": by_source}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    deprecated = skipped = 0
    for it in items:
        if not it.get("_deprecate_reason"):
            continue
        nid = ids[it["ref"]]
        s, cur = api.call("GET", f"/logs/{nid}", ws=ws)
        if s == 200 and cur.get("status") == "deprecated":
            skipped += 1
            continue
        s, r = api.call("PATCH", f"/logs/{nid}", {"status": "deprecated", "reason": it["_deprecate_reason"]}, ws)
        if s != 200:
            sys.exit(f"{path.name}: {it['ref']}(id {nid}) 폐기 실패 {s} {r}")
        deprecated += 1
    print(f"{path.name}: ws{ws} created={res['created']} existing={res['existing']} deprecated={deprecated}"
          f"{f' (이미 폐기 {skipped})' if skipped else ''} → {out.relative_to(ROOT)}")
    return res


ORDER = ["hr-ga_v1", "sw-dev_v1", "cs_v1", "it-cs_v1", "it-dev_v1"]   # 먼저 만든 세트부터 — 새 세트가 끼어들어 기존 id 가 밀리지 않게
IT_USER = "OIT0001"   # IT 회사(부서 20번대)의 시드 계정


def default_files() -> list[pathlib.Path]:
    found = {p.stem: p for p in (ROOT / "data" / "logs").glob("*_v1.json")}
    return [found.pop(n) for n in ORDER if n in found] + [found[n] for n in sorted(found)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--base", default=f"http://localhost:{os.environ.get('PORT', '8080')}/api/v1")
    ap.add_argument("--user", default="OOM0002")
    a = ap.parse_args()
    files = [pathlib.Path(f) for f in a.files] or default_files()
    if not files:
        sys.exit(f"적재할 파일이 없습니다: {ROOT / 'data' / 'logs'}/*_v1.json — 브랜치가 data/synthetic-logs 인지 확인")
    sessions = {}

    def session(user: str) -> Api:   # 회사마다 계정이 다르다 — 파일의 부서가 속한 회사의 계정으로 로그인
        if user not in sessions:
            api = Api(a.base)
            s, r = api.call("POST", "/auth/login", {"employee_no": user, "password": os.environ.get("SEED_PASSWORD", "Oom!demo-2026")})
            if s != 200:
                sys.exit(f"로그인 실패({user}) {s} {r}")
            sessions[user] = api
        return sessions[user]

    tot = {"created": 0, "existing": 0}
    for f in files:
        ws = json.loads(f.read_text(encoding="utf-8"))["workspace_id"]
        res = load_file(session(IT_USER if ws >= 20 else a.user), f)
        tot["created"] += res["created"]; tot["existing"] += res["existing"]
    print(f"합계 created={tot['created']} existing={tot['existing']}  → 다음: make worker ARGS=--once")


if __name__ == "__main__":
    main()
