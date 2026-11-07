"""사례 체인 기록 구조 2단계: 원인 표시와 원인 → 해결 짝."""
from contextlib import nullcontext

from app.graph import pipeline
from app.llm import client as llm


def _chain(monkeypatch, resolving=frozenset({5, 6})):
    # 장애 건 1 → 원인 2(메일 앱) → 해결 5 / 원인 3(연휴 망각) → 해결 6 / 시도 4(원인 아님) → 후속 7(2·3 공통)
    nodes = [{"id": i, "type": t, "status": "active", "title": f"n{i}", "depth": d, "path_label": None, "last_confirmed_at": None, "is_cause": c}
             for i, t, d, c in ((1, "fail", 0, False), (2, "fail", 1, True), (3, "fail", 1, True), (4, "fail", 1, False),
                                (5, "success", 2, False), (6, "success", 2, False), (7, "success", 2, False))]
    edges = [{"parent_id": 1, "child_id": c, "reason": None} for c in (2, 3, 4)]
    edges += [{"parent_id": 2, "child_id": 5, "reason": None}, {"parent_id": 3, "child_id": 6, "reason": None},
              {"parent_id": 3, "child_id": 7, "reason": None}, {"parent_id": 2, "child_id": 7, "reason": None}]
    monkeypatch.setattr(pipeline.db, "connect", lambda: nullcontext())
    monkeypatch.setattr(pipeline.db, "root_of", lambda conn, ws, e: 1)
    monkeypatch.setattr(pipeline.db, "chain", lambda conn, ws, root, d, n: {"entry_id": root, "nodes": nodes, "edges": edges})
    monkeypatch.setattr(pipeline.db, "resolved_nodes", lambda conn, ws, root: set(resolving))


def test_load_chain_carries_cause_and_parent_markers(monkeypatch):
    _chain(monkeypatch)
    ctx, ev = pipeline._load_chain(1, 1)
    by = {n["id"]: n for n in ctx}
    assert [by[i]["cause"] for i in (1, 2, 3, 4)] == [False, True, True, False]
    assert by[5]["parents"] == ["N2"] and by[7]["parents"] == ["N2", "N3"], "상위 마커는 번호순"
    assert [n.is_cause for n in ev.nodes] == [False, True, True, False, False, False, False]


def test_render_marks_cause_and_names_parent_from_depth_two(monkeypatch):
    _chain(monkeypatch)
    ctx, _ = pipeline._load_chain(1, 1)
    text = llm._render_context([], ctx)
    assert "[N2] (fail, 원인) n2" in text and "[N4] (fail) n4" in text
    assert "[N5] (success) n5 (상위: [N2])" in text and "[N7] (success) n7 (상위: [N2] [N3])" in text
    assert "[N2] (fail, 원인) n2 (상위:" not in text, "깊이 1 은 장애 건 바로 아래라 상위를 적지 않는다"


def test_must_include_pairs_each_resolution_with_its_cause(monkeypatch):
    _chain(monkeypatch)
    ctx, _ = pipeline._load_chain(1, 1)
    must = llm._must_include(ctx, asks_cause=True)
    pair = next(m for m in must if m.startswith("이 장애는 원인이 여럿이다"))
    assert "원인 [N2] → 해결 [N5]" in pair and "원인 [N3] → 해결 [N6]" in pair
    assert "[N7]" not in pair, "해결 기록이 있으면 후속 조치는 짝에 넣지 않는다"


def test_no_pair_line_when_the_question_enters_one_cause(monkeypatch):
    """질문이 한 원인(연휴 망각)으로 들어오면 짝은 하나뿐 — 상위 표시로 충분해 지시를 더하지 않는다."""
    _chain(monkeypatch)
    ctx, _ = pipeline._load_chain(1, 3)
    assert not any(m.startswith("이 장애는 원인이 여럿이다") for m in llm._must_include(ctx, asks_cause=True))


def test_pair_line_comes_last_and_only_when_the_question_asks_cause(monkeypatch):
    _chain(monkeypatch)
    ctx, _ = pipeline._load_chain(1, 1)
    assert llm._must_include(ctx, asks_cause=True)[-1].startswith("이 장애는 원인이 여럿이다")
    assert not any(m.startswith("이 장애는 원인이 여럿이다") for m in llm._must_include(ctx)), "해결법을 묻는 질문에는 붙이지 않는다"


def test_no_pair_line_without_cause_marks(monkeypatch):
    _chain(monkeypatch)
    ctx, _ = pipeline._load_chain(1, 1)
    for n in ctx:
        n["cause"] = False
    assert not any(m.startswith("이 장애는 원인이 여럿이다") for m in llm._must_include(ctx, asks_cause=True))
    assert not any(m.startswith("이 장애는 원인이 여럿이다") for m in llm._must_include(ctx, case_required=False, asks_cause=True))


def test_template_answer_lists_marked_causes_not_reference_logs(monkeypatch):
    _chain(monkeypatch)
    ctx, _ = pipeline._load_chain(1, 1)
    text = "".join(llm._template_answer("잠겼어", [], ctx))
    assert "원인: n2 [N2], n3 [N3]" in text
