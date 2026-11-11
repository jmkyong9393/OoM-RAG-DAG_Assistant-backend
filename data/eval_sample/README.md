# 대표 묶음 — 변경을 확인할 때 전체 대신 돌리는 약 10%

전체 회귀(평가셋 452 + 점검표 48×2 + 개정 5×2 + 품질 채점 48)는 한 번에 OpenAI 약 2달러가 든다. 변경을 확인할 때는 이 묶음만 돌린다(약 0.3달러).

| 파일 | 내용 | 고른 법 |
|---|---|---|
| `evalset_10pct_v1.json` | 평가셋 452문항 중 43문항(규정 30 · 사례 13, 8개 부서) | (부서, 질문 종류)별 비례 배분. 칸마다 절반까지는 지난 실행에서 결과가 갈린 문항(인용 안 함 · 거짓 거절 · 놓침 · 빈 답)을 먼저, 나머지는 고정된 씨앗으로 |
| `checks_core_v1.json` | 답변 점검표 48항목 중 11항목 | 어려운 문항(`hard_v1`) 전부 + 한 번이라도 떨어진 적 있는 문항 + 빠진 부서에서 1항목씩 |

```bash
cd ai
uv run --extra graph python ../eval/answer_on_evalset.py ../data/eval_sample/evalset_10pct_v1.json --json <결과>
uv run --extra graph python ../eval/answer_checks.py ../data/eval_sample/checks_core_v1.json --runs 1 --json <결과>
AI_DB_URL=<복제 DB> uv run --extra graph python ../eval/answer_checks.py ../data/answer_checks/revisions/revised_v1.json --runs 1 --json <결과>
uv run --extra graph python ../eval/answer_quality.py ../data/eval_sample/checks_core_v1.json --judge-model gpt-4.1 --json <결과>
```

결과가 갈리던 문항을 일부러 많이 넣었으므로 통과율을 전체의 추정치로 읽으면 안 된다 — 같은 묶음의 앞뒤 실행을 견주는 용도다.
