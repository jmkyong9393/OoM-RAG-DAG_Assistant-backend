# data/policies — 규정 문서 청크 JSON (1급 자산)

원문(PDF·HWP·Markdown)에서 변환한 **조문 단위 청크**. 이 파일이 규정 검색의 원천이며 `scripts/index_policy.py` 가 DB 에 적재한다.
PDF 하이라이트가 필요하면 `bboxes` 에 사각형 목록을 넣고 `file_path` 로 원문을 가리킨다.

```json
{
  "slug": "vpn-guide-v22",              // 전역 유일. 청크 id 접두어
  "workspace_id": 2,                    // 규정을 소유한 워크스페이스 (전사 공통이면 10)
  "title": "VPN·원격접속 운영지침",
  "version": "v2.2",
  "doc_type": "지침",                   // 규정·지침·FAQ·매뉴얼·약관
  "source_type": "markdown",            // pdf|hwp|markdown|json
  "file_path": null,
  "effective_from": "2026-11-01",
  "supersedes": "vpn-guide-v21",        // 선택. 없으면 같은 workspace·title 의 active 문서를 자동 폐기 (R8)
  "tags": ["보안", "원격근무"],
  "chunks": [
    {"id": "vpn-guide-v22-a12-1",       // 선택. 없으면 "{slug}-001"
     "article_label": "제12조 (원격접속 신청)", "article_title": null, "page": 7,
     "text": "① 사외에서 사내망에 접속하려는 임직원은 …", "bboxes": null}
  ]
}
```

## 적재 흐름 (D-4) — `make index` 한 번

```
data/policies/src/<slug>/            ← 집필자(프론트팀 §A-1). slug 는 폴더명 = meta.json slug = 청크 id 접두어
├── <slug>.md                        원문 정본
├── meta.json                        slug·workspace_id·title·version·doc_type·source_type·file_path·effective_from·supersedes·tags·terms·author·summary
├── <slug>.questions.md              평가 질의 15건 → data/evalset/<slug>_v1.json (D-4 ④)
└── <slug>.pdf                       PDF 문서만. 쪽 번호는 자동 추출
```

`make index` 는 `src/*/meta.json` 을 순회해 ① `build_policy_json.py --meta` 로 `<slug>.chunks.json` 생성(결정적 — 같은 원문이면 같은 파일)
② `<slug>.pdf` 가 있으면 `pdf_locate.py --write` 로 조문 쪽 번호(`page`)와 라벨 줄 사각형(`bboxes`) ③ `index_policy.py` 로 DB 적재(해시 같으면 스킵, R8 개정 폐기,
`terms` → `workspace_terms(source='manual')`) ④ `app.worker --once --reindex` 로 토큰·임베딩 백필 — **`--reindex` 인 이유**: kiwi 사용자 사전은 전 워크스페이스
합집합 하나라 새 `terms` 가 들어오면 기존 청크·노드도 다시 토크나이즈해야 한다(임베딩은 재사용).

- `make pdf SLUG=<slug>`: 쪽 번호만 다시 채우고 PDF 를 서빙 위치 `data/files/policies/<slug>.pdf` 로 복사(`file_path: policies/<slug>.pdf`). 서빙 API 는 게이트 이후.
- 못 찾은 조문은 `page: null` 로 남고 목록이 출력된다 — PDF 의 조문 제목 줄이 Markdown 과 글자 단위로 같아야 한다(§A-4).
- 변환기만 돌려 보기(집필 자가 점검): `uv run scripts/build_policy_json.py --meta data/policies/src/<slug>/meta.json --out build/check`

원문에서 만들기(인자 직접): `uv run scripts/build_policy_json.py 원문.md --slug <slug> --workspace <id> --title "<제목>" --version v1.0 [--supersedes <구 slug>]`
(`제N조`·`제N조의M`·`Q18.`·`## 제목`·`[별표]` 줄을 경계로 자르고, 긴 조문은 항 ①②③ 경계에서 `--max-chars`(기본 700) 이하로 나눈다.
표(`| 셀 | 셀 |` 행)가 있는 절은 행 경계에서 나눈다)

HWPX(한글) 원문이면 먼저 `uv run scripts/hwpx_to_md.py 별표1.hwpx 별표2.hwpx … --prelude 본문.md --out 원문.md` 로 Markdown 을 만든다.
업종 제목(`1. 농ㆍ수ㆍ축산물`)은 `##`, 그 아래 업종·품목 소제목(`< ①운수업 >` 문단, `⑤자 동 차 (2-1)` 병합 행)은 `###`/`####` 제목으로, 표는 행마다 한 줄로 옮긴다.
`build_policy_json.py` 는 `###`/`####` 를 상위 `##` label 뒤에 ` › ` 로 이어 붙인다(예: `별표 Ⅱ 상품 › 5. 공산품(34개 품목) › ⑤자동차`). 원문과 변환 결과는 `src/<slug>/` 에 함께 둔다.

| slug | 원문 | 청크 | 비고 |
|---|---|---|---|
| `consumer-dispute-2025` | `src/consumer-dispute-2025/` 공정위고시 제2025-14호 본문 + 별표 Ⅰ~Ⅳ (law.go.kr HWPX) | 284 (label 129 — 10/29 표 행 분할 규칙 반영본) | 워크스페이스 3(CS). 시드 `consumer-dispute` 를 폐기(`--supersedes`) |
| `vpn-guide-v22` | (시드) | 3 | 워크스페이스 2 |

규칙
- `index_policy.py` 는 제목·버전·청크 본문의 해시가 같으면 스킵, 다르면 청크를 교체한다(재실행 멱등).
- 개정판을 넣으면 구버전은 `retired` 가 되고 검색에서 빠진다(`retire_reason='v2.2 개정'`, 감사 로그 `superseded`). 구버전 청크는 남아 "이전 규정" 재열람이 가능하다.
- 적재 후 `make worker ARGS=--once` 로 토큰·임베딩을 채워야 검색된다(`make index` 가 둘을 이어서 실행).
- 청크 하나는 조문 하나(또는 FAQ 한 항목)가 기본. 조문이 길면 항(①②) 단위로 나누고 `article_label` 은 같게 둔다.
