"""실제 LLM 공급자 1회 점검 (키 발급 후). deploy/.env 의 LLM_PROFILE_DEFAULT_* 로 classify · generate(스트리밍) · refine · embeddings 를 각 1회 호출해
응답·usage·소요 시간을 찍는다. DB 는 쓰지 않는다.

실행: set -a; . deploy/.env; set +a; cd ai && uv run python ../scripts/llm_smoke.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai"))
os.environ["AI_MODE"] = "openai"

from app.config import profile  # noqa: E402
from app.llm import client as llm  # noqa: E402
from app.retrieval import embed  # noqa: E402
from app.retrieval.tokenize import Tokenizer  # noqa: E402


async def main() -> int:
    prof = profile(os.environ.get("LLM_PROFILE", "default"))
    if not prof.has_key:
        print(f"LLM_PROFILE_{prof.name.upper()}_API_KEY 가 비어 있습니다"); return 2
    print(f"profile={prof.name} base={prof.base_url} light={prof.model_light} answer={prof.model_answer} embed={prof.embedding_model}")
    u = llm.Usage()
    ok = True

    t = time.perf_counter()
    try:
        r = await llm.classify(prof, "VPN 접속 시 E-403 오류가 반복되는데 규정상 어떻게 조치해야 하나요?", "E-403", True, u)
        print(f"[classify] {1000*(time.perf_counter()-t):.0f} ms → {r.model_dump()}")
    except Exception as e:  # noqa: BLE001
        ok = False; print(f"[classify] FAIL {type(e).__name__}: {e}")

    t = time.perf_counter(); first = None; n = 0
    policy = [{"ref": "P1", "chunk_id": "x", "doc_title": "VPN·원격접속 운영지침", "article_label": "제12조", "text": "사외 접속은 사전 신청 후 승인된 기간에만 허용한다."}]
    chain = [{"ref": "N1", "id": 1, "type": "fail", "status": "active", "title": "VPN E-403 반복", "resolution_summary": None, "depth": 0},
             {"ref": "N2", "id": 2, "type": "success", "status": "active", "title": "갱신 주기 조정", "resolution_summary": "토큰 갱신 배치 주기를 30분으로 조정", "depth": 1}]
    try:
        out = []
        async for piece in llm.generate(prof, "E-403 어떻게 해결했고 규정은 뭐야?", policy, chain, u):
            if first is None: first = time.perf_counter() - t
            out.append(piece); n += 1
        print(f"[generate] first_token {1000*first:.0f} ms, total {1000*(time.perf_counter()-t):.0f} ms, chunks {n}\n  {''.join(out)[:300]}")
    except Exception as e:  # noqa: BLE001
        ok = False; print(f"[generate] FAIL {type(e).__name__}: {e}")

    t = time.perf_counter()
    try:
        r = await llm.refine(prof, "FAQ 연동 API 타임아웃(504)", "FAQ API 호출이 HTTP 504 로 실패했다. 커넥션 풀을 20→50 으로 늘리고 타임아웃을 3s 로 조정해 해결.", Tokenizer(), u)
        print(f"[refine] {1000*(time.perf_counter()-t):.0f} ms → {r.model_dump()}")
    except Exception as e:  # noqa: BLE001
        ok = False; print(f"[refine] FAIL {type(e).__name__}: {e}")

    t = time.perf_counter()
    try:
        v = embed.embed(prof, ["VPN 접속 오류", "환불 규정"])
        dims = {len(x) for x in v}
        print(f"[embeddings] {1000*(time.perf_counter()-t):.0f} ms → {len(v)} vectors, dims {dims}" + ("" if dims == {1536} else "  ← 1536 이 아니면 스키마와 불일치!"))
        ok &= dims == {1536}
    except Exception as e:  # noqa: BLE001
        ok = False; print(f"[embeddings] FAIL {type(e).__name__}: {e}")

    print(f"usage: prompt={u.prompt_tokens} completion={u.completion_tokens}")
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
