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

원문에서 만들기: `uv run scripts/build_policy_json.py 원문.md --slug <slug> --workspace <id> --title "<제목>" --version v1.0 [--supersedes <구 slug>]`
(`제N조`·`제N조의M`·`Q18.`·`## 제목`·`[별표]` 줄을 경계로 자르고, 긴 조문은 항 ①②③ 경계에서 `--max-chars`(기본 700) 이하로 나눈다)

규칙
- `index_policy.py` 는 제목·버전·청크 본문의 해시가 같으면 스킵, 다르면 청크를 교체한다(재실행 멱등).
- 개정판을 넣으면 구버전은 `retired` 가 되고 검색에서 빠진다(`retire_reason='v2.2 개정'`, 감사 로그 `superseded`). 구버전 청크는 남아 "이전 규정" 재열람이 가능하다.
- 적재 후 `make worker ARGS=--once` 로 토큰·임베딩을 채워야 검색된다(`make index` 가 둘을 이어서 실행).
- 청크 하나는 조문 하나(또는 FAQ 한 항목)가 기본. 조문이 길면 항(①②) 단위로 나누고 `article_label` 은 같게 둔다.
