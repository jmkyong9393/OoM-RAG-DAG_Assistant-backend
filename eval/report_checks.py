"""장애 보고서(R1) AI 칸 점검 — 실제 보고서를 만들어, LLM 이 쓴 문장이 원인 · 해결 기록을 빠뜨리지 않았는지 세고 입력 · 출력을 파일로 남긴다 (LLM 호출 있음).

대상: 하위 기록이 있는 장애 건(루트, 활성 fail) 전부. 건마다 POST /reports(같은 내용이면 저장된 보고서를 다시 쓴다) → 대기 중인 보고서를 이 프로세스에서 채운다(워커와 같은 경로).
--refill: 저장된 보고서의 사실 칸으로 AI 칸만 다시 만든다(DB 에 쓰지 않는다) — ai/app/report.py 의 지시문을 고친 뒤 같은 입력으로 비교할 때.
세는 것(호출 없음): 원인 설명이 붙은 원인 기록 수, 한눈에 보기가 언급한 원인 · 해결 기록 수(근거 번호 기준 / 제목 낱말 기준), 한 항목에 여러 문장을 넣은 수, 길이, 토큰.
  제목 낱말 기준은 바꿔 말한 표현을 놓친다 — 빠짐으로 나온 것은 --list 로 뽑아 요약과 대조해 확정한다. 기록에 없는 말은 세지 못한다: --json 파일의 input · ai 를 읽어 확인한다.
  해결 요약의 근거(해결 기록이 없음 · 일부만 댐)도 센다. 작은 호출(해결 문장 다시 받기 · 재발 방지 보완)이 돈 수와 더한 문장 · 버린 문장은 --refill 일 때만 센다
  (저장된 보고서에는 그 기록이 없다) — --json 의 small_calls 에 건마다 넘긴 기록 · 더한 문장 · 버린 문장과 이유가 남는다.
  번호별 칸(원인 설명 · 추가 문제 메모)이 빠져 본 호출을 한 번 더 부른 보고서 수와 채운 칸 수도 같이 센다.

실행: set -a; . deploy/.env; set +a; cd ai && uv run python ../eval/report_checks.py [--refill] [--ws 1] [--limit 5] [--list] [--json 결과경로]
      (api 가 떠 있어야 한다. PORT · SEED_PASSWORD 는 deploy/.env)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai"))

from app import report  # noqa: E402
from app.config import profile as load_profile  # noqa: E402
from app.llm import client as llm  # noqa: E402
from app.retrieval import db  # noqa: E402

WORD = re.compile(r"[가-힣A-Za-z0-9]{2,}")
SENTENCE_END = re.compile(r"다\.(?:\s|$)")


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
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, {"message": e.read().decode("utf-8", "replace")[:300]}


def facts_only(content: dict) -> dict:
    """AI 칸을 비운 content — 같은 사실 칸으로 다시 채울 때의 입력."""
    return {**content, "summary": [], "impact": [], "prevention": [],
            "causes": [{**c, "explanation": None} for c in content["causes"]],
            "side_issues": [{**s, "note": None} for s in content["side_issues"]],
            "resolution": {**content["resolution"], "note": None}}


def without_fixes(content: dict) -> dict:
    """해결 기록 지정을 뺀 사본 — 진행 중인 장애 건을 흉내 낸다(해결 기록은 조치로, 상태는 진행 중으로). DB 는 건드리지 않는다."""
    fixed = {i["ref"] for i in content["resolution"]["items"]}
    return {**content, "meta": {**content["meta"], "status": "open", "interim": True, "resolved_at": None},
            "records": [{**r, "kind": "action"} if r["ref"] in fixed else r for r in content["records"]],
            "resolution": {**content["resolution"], "items": []}}


def mentions(title: str, text: str) -> bool:
    """제목의 낱말(2자 이상) 절반 이상이 글에 그대로 나온다."""
    words = WORD.findall(title)
    return bool(words) and sum(w in text for w in words) * 2 >= len(words)


def score(content: dict) -> dict:
    """보고서 한 건의 점검 값. *_by_ref = 한눈에 보기의 근거 번호에 그 기록이 있다, *_by_text = 제목 낱말이 한눈에 보기에 나온다."""
    title = {r["ref"]: r["title"] for r in content["records"]}
    causes = [c["ref"] for c in content["causes"]]
    fixes = [i["ref"] for i in content["resolution"]["items"]]
    text = " ".join(s["text"] for s in content["summary"])
    cited = {r for s in content["summary"] for r in s["refs"]}
    missing = lambda refs, by_ref: [r for r in refs if not (r in cited if by_ref else (r in cited or mentions(title[r], text)))]  # noqa: E731
    sentences = content["summary"] + content["impact"] + content["prevention"] + content["open_items"]
    note_refs = set((content["resolution"].get("note") or {}).get("refs") or [])
    # AI 가 쓴 문장 전부(한눈에 보기 · 영향 · 재발 방지 · 원인 설명 · 추가 문제 메모 · 해결 요약) — 남은 확인(open_items)은 사실 칸이라 뺀다
    written = (content["summary"] + content["impact"] + content["prevention"]
               + [x for x in [c.get("explanation") for c in content["causes"]] + [s.get("note") for s in content["side_issues"]]
                  + [content["resolution"].get("note")] if x])
    return {"records": len(content["records"]), "causes": len(causes), "fixes": len(fixes),
            "causes_explained": sum(1 for c in content["causes"] if c.get("explanation")),
            "side_issues": len(content["side_issues"]), "side_issues_noted": sum(1 for s in content["side_issues"] if s.get("note")),
            "causes_missing_by_ref": missing(causes, True), "causes_missing_by_text": missing(causes, False),
            "fixes_missing_by_ref": missing(fixes, True), "fixes_missing_by_text": missing(fixes, False),
            "resolution_note": bool(content["resolution"].get("note")) if fixes else None,
            # 해결 요약의 근거 — 해결 기록이 하나도 없는 것(싣지 않아야 한다) · 해결 기록이 여럿인데 일부만 댄 것(참고용)
            "note_without_fix_ref": int(bool(note_refs) and not note_refs.intersection(fixes)),
            "multi_fix": int(len(fixes) > 1), "note_partial_fix_refs": int(len(fixes) > 1 and bool(note_refs) and not note_refs.issuperset(fixes)),
            "multi_sentence_items": sum(1 for s in sentences if len(SENTENCE_END.findall(s["text"])) > 1),
            # 메모 말투('~안 됨.' · '~있었음.' · 명사로 끝남) — 기록 제목을 그대로 옮기면 생긴다
            "not_polite_ending": sum(1 for s in written if not s["text"].rstrip().rstrip(".").endswith("니다")),
            "summary_items": len(content["summary"]), "summary_chars": len(text),
            "prevention": len(content["prevention"]), "open_items": len(content["open_items"]), "impact": len(content["impact"]),
            # 쓰지 말아야 할 문장 — 원인 기록이 없는데 한눈에 보기가 세 문장(원인 문장이 낌) · 해결 기록이 없는데 고정 문장이 아니거나 해결 요약이 있음
            "cause_without_record": int(not causes and len(content["summary"]) > 2),
            "fix_without_record": int(not fixes and bool(content["summary"]) and (content["summary"][-1]["text"] not in (report.NO_FIX_OPEN, report.NO_FIX_DONE)
                                                                                 or content["resolution"].get("note") is not None))}


def small_call_totals(rows: list[dict]) -> dict:
    """작은 호출의 집계 — report.fill 에 넘긴 trace(row["small_calls"])에서 센다. trace 가 없는 행(저장된 보고서)은 0 으로 센다."""
    fix = [r["small_calls"]["fix"] for r in rows if (r.get("small_calls") or {}).get("fix")]
    more = [r["small_calls"]["prevention"] for r in rows if (r.get("small_calls") or {}).get("prevention")]
    dropped: dict[str, int] = {}
    for m in more:
        for why, _ in m["dropped"]:
            dropped[why] = dropped.get(why, 0) + 1
    notes = [r["small_calls"]["notes"] for r in rows if (r.get("small_calls") or {}).get("notes")]
    main_dropped: dict[str, int] = {}   # 본 호출의 재발 방지 문장 가운데 검증이 버린 것(이유별)
    for r in rows:
        for why, _ in ((r.get("small_calls") or {}).get("main_prevention") or {}).get("dropped", []):
            main_dropped[why] = main_dropped.get(why, 0) + 1
    marked = [r["small_calls"]["marked"] for r in rows if (r.get("small_calls") or {}).get("marked")]   # 팀이 재발 방지 기록을 표시한 장애 건
    return {"main_prevention_dropped": main_dropped, "marked_reports": len(marked), "marked_records": sum(len(m["marked"]) for m in marked),
            "marked_asked_again": sum(1 for m in marked for v in m["raw"].values() if len(v) > 1), "marked_title_fallback": sum(len(m["fallback"]) for m in marked),
            "notes_refilled_reports": len(notes), "notes_missing": sum(len(n["missing"]) for n in notes), "notes_filled": sum(len(n["filled"]) for n in notes),
            "fix_refetched": len(fix), "fix_refetch_accepted": sum(1 for f in fix if f["accepted"]),
            "prevention_supplemented": len(more), "prevention_added": sum(len(m["added"]) for m in more), "prevention_dropped": dropped,
            "small_call_errors": sum(1 for x in fix + more + notes if x.get("error"))}


def totals(rows: list[dict]) -> dict:
    ok = [r["score"] for r in rows if r.get("score")]
    add = lambda k: sum(len(s[k]) if isinstance(s[k], list) else int(s[k] or 0) for s in ok)  # noqa: E731
    return {**small_call_totals(rows), "note_without_fix_ref": add("note_without_fix_ref"), "multi_fix": add("multi_fix"),
            "note_partial_fix_refs": add("note_partial_fix_refs"),
            "reports": len(rows), "failed": sum(1 for r in rows if r.get("error")), "empty": sum(1 for s in ok if not s["summary_items"]),
            "causes": add("causes"), "causes_explained": add("causes_explained"),
            "causes_missing_by_ref": add("causes_missing_by_ref"), "causes_missing_by_text": add("causes_missing_by_text"),
            "fixes": add("fixes"), "fixes_missing_by_ref": add("fixes_missing_by_ref"), "fixes_missing_by_text": add("fixes_missing_by_text"),
            "full_by_text": sum(1 for s in ok if s["summary_items"] and not s["causes_missing_by_text"] and not s["fixes_missing_by_text"]),
            "side_issues": add("side_issues"), "side_issues_noted": add("side_issues_noted"),
            "multi_sentence_items": add("multi_sentence_items"), "not_polite_ending": add("not_polite_ending"),
            "prevention": add("prevention"), "prevention_reports": sum(1 for s in ok if s["prevention"]), "impact": add("impact"),
            "no_cause_reports": sum(1 for s in ok if not s["causes"]), "cause_without_record": add("cause_without_record"),
            "no_fix_reports": sum(1 for s in ok if not s["fixes"]), "fix_without_record": add("fix_without_record"),
            "summary_chars_avg": round(sum(s["summary_chars"] for s in ok) / len(ok)) if ok else 0,
            "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rows), "completion_tokens": sum(r.get("completion_tokens") or 0 for r in rows)}


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--refill", action="store_true", help="저장된 보고서의 사실 칸으로 AI 칸만 다시 만든다(DB 에 쓰지 않음)")
    ap.add_argument("--ws", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--list", action="store_true", help="빠짐으로 나온 기록과 한눈에 보기를 건마다 찍는다")
    ap.add_argument("--open-sample", type=int, default=0, help="완료된 장애 건 N개에서 해결 기록 지정을 뺀 사본으로 AI 칸을 만들어 본다(진행 중 표본, DB 에 쓰지 않음)")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--base", default=f"http://localhost:{os.environ.get('PORT', '8080')}/api/v1")
    ap.add_argument("--allow-stale-schema", action="store_true")
    a = ap.parse_args()
    import schema_check
    schema_check.ensure_current(a.allow_stale_schema)
    password = os.environ.get("SEED_PASSWORD", "Oom!demo-2026")
    apis: dict[str, Api] = {}
    rows: list[dict] = []
    raw: dict = {}
    chat = llm._chat_json

    async def capture(*args, **kw):   # 검증(merge)을 거치기 전의 본 호출 응답을 남긴다 — 칸이 비었을 때 AI 가 안 쓴 것인지 검증이 버린 것인지 가린다
        answer = await chat(*args, **kw)
        raw.setdefault("main", answer)   # 한 보고서의 첫 호출이 본 호출이다. 작은 호출의 응답은 trace 에 남는다
        return answer

    llm._chat_json = capture
    open_rows: list[dict] = []
    kept: list[tuple] = []   # (row, 사실 칸, 본문) — 진행 중 표본용
    with db.connect() as conn:
        roots = conn.execute("""
            SELECT v.id, v.workspace_id, v.title,
                   (SELECT u.employee_no FROM workspace_members m JOIN users u ON u.id = m.user_id
                    WHERE m.workspace_id = v.workspace_id ORDER BY (m.role = 'admin') DESC, u.id LIMIT 1) AS employee_no
            FROM v_log_nodes v
            WHERE v.root_id = v.id AND v.type = 'fail' AND v.status = 'active' AND (%s::int IS NULL OR v.workspace_id = %s)
              AND EXISTS (SELECT 1 FROM log_edges e WHERE e.parent_id = v.id)
            ORDER BY v.workspace_id, v.id""", (a.ws, a.ws)).fetchall()[: a.limit]
        conn.rollback()
        for r in roots:
            api = apis.get(r["employee_no"])
            if api is None:
                api = apis[r["employee_no"]] = Api(a.base)
                s, body = api.call("POST", "/auth/login", {"employee_no": r["employee_no"], "password": password})
                if s != 200:
                    sys.exit(f"로그인 실패 {r['employee_no']}: {s} {body}")
            s, body = api.call("POST", "/reports", {"root_id": r["id"], "audience": "internal", "force": False}, r["workspace_id"])
            if s != 200:
                rows.append({"root_id": r["id"], "workspace_id": r["workspace_id"], "title": r["title"], "error": f"POST /reports {s} {body}"})
                continue
            rows.append({"root_id": r["id"], "workspace_id": r["workspace_id"], "title": r["title"], "report_id": body["id"]})
        if not a.refill:
            filled = await report.run_pending(conn, limit=len(rows) + 50)   # 대기 중인 보고서를 워커와 같은 경로로 채운다
            print(f"채움: {filled}")
        for row in rows:
            if "report_id" not in row:
                continue
            rec = conn.execute("""SELECT status, content, error, prompt_version, model, prompt_tokens, completion_tokens
                                  FROM incident_reports WHERE id = %s""", (row["report_id"],)).fetchone()
            facts = facts_only(rec["content"])
            ids = [x["id"] for x in facts["records"]]
            bodies = {x["id"]: x["body"] for x in conn.execute("SELECT id, body FROM log_nodes WHERE id = ANY(%s)", (ids,)).fetchall()}
            row["input"] = report.render(facts, bodies)
            kept.append((row, facts, bodies))
            if a.refill:
                usage, trace = llm.Usage(), {}
                prof = load_profile(db.workspace_settings(conn, row["workspace_id"])["settings"].get("llm_profile", "default"))
                raw.pop("main", None)
                try:
                    content = await report.fill(prof, facts, bodies, False, usage, trace)
                except Exception as e:  # noqa: BLE001
                    row["error"] = f"{type(e).__name__}: {e}"[:300]
                    continue
                if content is None:
                    sys.exit("LLM 키가 없어 AI 칸을 만들 수 없습니다 (AI_MODE · LLM_PROFILE_DEFAULT_API_KEY)")
                row.update(model=prof.model_answer, prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens, raw=raw.pop("main", None),
                           small_calls=trace)
            else:
                content = rec["content"]
                row.update(status=rec["status"], model=rec["model"], prompt_version=rec["prompt_version"],
                           prompt_tokens=rec["prompt_tokens"], completion_tokens=rec["completion_tokens"])
                if rec["status"] != "done":
                    row["error"] = rec["error"] or f"status={rec['status']}"
                    continue
            conn.rollback()
            row["ai"] = {"summary": content["summary"], "impact": content["impact"], "prevention": content["prevention"], "open_items": content["open_items"],
                         "causes": {c["ref"]: c["explanation"] for c in content["causes"]},
                         "side_issues": {s["ref"]: s["note"] for s in content["side_issues"]}, "resolution_note": content["resolution"]["note"]}
            row["score"] = score(content)
        for row, facts, bodies in [k for k in kept if k[1]["resolution"]["items"]][: a.open_sample]:
            sample, usage, trace = without_fixes(facts), llm.Usage(), {}
            prof = load_profile(db.workspace_settings(conn, row["workspace_id"])["settings"].get("llm_profile", "default"))
            conn.rollback()
            raw.pop("main", None)
            content = await report.fill(prof, sample, bodies, False, usage, trace)
            if content is None:
                sys.exit("LLM 키가 없어 AI 칸을 만들 수 없습니다 (AI_MODE · LLM_PROFILE_DEFAULT_API_KEY)")
            open_rows.append({"root_id": row["root_id"], "workspace_id": row["workspace_id"], "title": row["title"], "input": report.render(sample, bodies),
                              "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens, "raw": raw.pop("main", None), "small_calls": trace,
                              "ai": {"summary": content["summary"], "resolution_note": content["resolution"]["note"]}, "score": score(content)})

    t = totals(rows)
    for row in rows:
        s = row.get("score")
        if row.get("error"):
            print(f"✗ ws{row['workspace_id']} #{row['root_id']} {row['title']}  ← {row['error']}")
        elif a.list and s and (s["causes_missing_by_text"] or s["fixes_missing_by_text"] or s["multi_sentence_items"] or s["causes_explained"] < s["causes"]):
            titles = dict(re.findall(r"^(N\d+) \([^)]*\) (.*?)(?: — | ← |$)", row["input"], re.M))
            print(f"△ ws{row['workspace_id']} #{row['root_id']} {row['title']}")
            for label, key in (("원인", "causes_missing_by_text"), ("해결", "fixes_missing_by_text")):
                for ref in s[key]:
                    print(f"    한눈에 보기에 없는 {label}: {ref} {titles.get(ref, '')}")
            if s["causes_explained"] < s["causes"]:
                print(f"    설명 없는 원인 {s['causes'] - s['causes_explained']}건")
            if s["multi_sentence_items"]:
                print(f"    여러 문장을 한 항목에 넣음 {s['multi_sentence_items']}건")
            print("    한눈에 보기: " + " ".join(x["text"] for x in row["ai"]["summary"]))
    print(f"\n보고서 {t['reports']}건 (실패 {t['failed']}, 한눈에 보기 없음 {t['empty']})")
    print(f"원인 설명 {t['causes_explained']}/{t['causes']}   추가 문제 메모 {t['side_issues_noted']}/{t['side_issues']}")
    print(f"한눈에 보기가 언급한 원인  번호 {t['causes'] - t['causes_missing_by_ref']}/{t['causes']}  낱말 {t['causes'] - t['causes_missing_by_text']}/{t['causes']}")
    print(f"한눈에 보기가 언급한 해결  번호 {t['fixes'] - t['fixes_missing_by_ref']}/{t['fixes']}  낱말 {t['fixes'] - t['fixes_missing_by_text']}/{t['fixes']}")
    print(f"원인 · 해결을 모두 언급한 보고서(낱말) {t['full_by_text']}/{t['reports'] - t['failed']}   여러 문장 항목 {t['multi_sentence_items']}   "
          f"한눈에 보기 평균 {t['summary_chars_avg']}자   토큰 입력 {t['prompt_tokens']} · 출력 {t['completion_tokens']}")
    print(f"재발 방지 문장 {t['prevention']}(보고서 {t['prevention_reports']}건)   영향 문장 {t['impact']}")
    print(f"원인 기록이 없는데 원인 문장 {t['cause_without_record']}/{t['no_cause_reports']}   해결 기록이 없는데 해결 문장 {t['fix_without_record']}/{t['no_fix_reports']}")
    print(f"'~니다'로 끝나지 않는 문장 {t['not_polite_ending']}")
    print(f"해결 요약의 근거에 해결 기록이 없음 {t['note_without_fix_ref']}   해결 기록이 여럿인데 일부만 근거로 댐 {t['note_partial_fix_refs']}/{t['multi_fix']}")
    if a.refill:
        why = " · ".join(f"{k} {v}" for k, v in t["prevention_dropped"].items()) or "없음"
        print(f"번호별 칸이 빠져 본 호출을 한 번 더 부른 보고서 {t['notes_refilled_reports']}건(빠진 칸 {t['notes_missing']} 중 채운 것 {t['notes_filled']})")
        print(f"해결 문장을 따로 다시 받은 보고서 {t['fix_refetched']}건(받아들인 것 {t['fix_refetch_accepted']})   "
              f"재발 방지 보완 호출 {t['prevention_supplemented']}건 · 더한 문장 {t['prevention_added']} · 버린 문장: {why}   작은 호출 실패 {t['small_call_errors']}")
    if open_rows:
        o = totals(open_rows)
        print(f"진행 중 표본 {o['reports']}건(해결 기록 지정을 뺀 사본): 해결 기록이 없는데 해결 문장 {o['fix_without_record']}/{o['no_fix_reports']}   "
              f"원인 빠짐 {o['causes_missing_by_text']}/{o['causes']}   토큰 입력 {o['prompt_tokens']} · 출력 {o['completion_tokens']}")
        for r in open_rows:
            print(f"    #{r['root_id']} " + " ".join(x["text"] for x in r["ai"]["summary"]))
    if a.json:
        md5 = lambda s: hashlib.md5(s.encode("utf-8")).hexdigest()  # noqa: E731
        meta = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "refill": a.refill, "prompt_version": report.PROMPT_VERSION,
                "prompt_md5": md5(report.SYSTEM), "prompt": report.SYSTEM, "fix_prompt_md5": md5(report.SYSTEM_FIX),
                "prevention_prompt_md5": md5(report.SYSTEM_PREVENTION), "signals": report.SIGNALS}
        a.json.write_text(json.dumps({"meta": meta, "summary": t, "rows": rows, "open_sample": open_rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
