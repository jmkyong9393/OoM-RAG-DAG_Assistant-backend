"""규정 검색 재순위: 하이브리드 검색 상위 후보를 가벼운 모델이 '질문에 직접 답하는 조문' 순으로 다시 고른다.
구어·우회 표현 질문은 어휘·벡터 모두 문어체 조문과 멀어 정답이 6~20위에 머무는 일이 잦다.
모델이 고르지 않은 자리는 원래 순서로 채우고, 실패·시간 초과면 원래 순서를 그대로 쓴다.
검색 상위 KEEP 건은 최종 K 안에 남긴다 — 고정 없이는 원래 2~3위였던 정답을 밀어낸 경우가 있었다."""
from __future__ import annotations

import asyncio
import re

from app.config import ai_mode
from app.llm import client as llm

POOL = 20          # 재순위에 넘기는 후보 수
TIMEOUT = 4.0      # 초 — 넘기면 원래 순서
EXCERPT = 260      # 후보마다 넘기는 본문 글자 수 (160 으로 줄여도 지연은 그대로, MRR 만 떨어졌다)
K = 5              # 답변 근거로 쓰는 상위 수
KEEP = 2           # 검색 상위 몇 건을 최종 K 안에 남길 것인가

SYSTEM = ("You re-rank Korean company regulation excerpts for a Korean employee's question. "
          "Reply JSON only: {\"ids\": [numbers]} — up to 5 excerpt numbers that directly answer the question, best first. "
          "Pick the excerpt that states the rule, number, deadline or procedure the question asks about, even if the question uses casual "
          "or indirect words. Do not pick excerpts that are only on a related topic. Use only numbers from the list.")


def _excerpt(r: dict) -> str:
    t = re.sub(r"\s+", " ", r.get("text") or "").strip()
    return t if len(t) <= EXCERPT else t[:EXCERPT] + "…"


def picked_order(rows: list[dict], ids: list) -> list[dict]:
    """모델이 고른 번호(1부터) 순서 + 나머지는 원래 순서. 목록 밖 번호·중복은 버린다."""
    seen: list[int] = []
    for x in ids or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(rows) and i not in seen:
            seen.append(i)
    return [rows[i - 1] for i in seen] + [r for i, r in enumerate(rows, 1) if i not in seen]


def keep_top(order: list[dict], rows: list[dict], k: int = K, keep: int = KEEP) -> list[dict]:
    """검색 상위 keep 건이 재순위 상위 k 에 없으면 재순위가 가장 뒤에 둔 것(고정이 아닌 것)과 바꾼다."""
    head, pinned = order[:k], rows[:keep]
    for x in pinned:
        if x not in head:
            for j in range(len(head) - 1, -1, -1):
                if head[j] not in pinned:
                    head[j] = x
                    break
    return head + [r for r in order if r not in head]


async def rerank(profile, query: str, rows: list[dict], usage: llm.Usage) -> list[dict]:
    if ai_mode() != "openai" or not profile.has_key or len(rows) < 2:
        return rows
    listing = "\n".join(f"[{i}] {r.get('doc_title', '')} {r.get('article_label', '')}: {_excerpt(r)}" for i, r in enumerate(rows, 1))
    try:
        data = await asyncio.wait_for(
            llm._chat_json(profile, profile.model_light, SYSTEM, f"[질문]\n{query}\n\n[조문]\n{listing}", usage), TIMEOUT)
    except Exception:  # noqa: BLE001 — 재순위는 보조 단계: 실패하면 검색 순서로 답한다
        return rows
    return keep_top(picked_order(rows, data.get("ids") if isinstance(data, dict) else None), rows)
