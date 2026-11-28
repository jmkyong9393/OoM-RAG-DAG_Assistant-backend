"""답변 스트림에서 근거 마커 없이 '따라서'로 다시 맺는 문장을 걸러 낸다.

경량 답변 모델은 근거 문장 뒤에 질문의 말을 되받은 결론을 마커 없이 덧붙이는데, 이 문장이 앞 문장과 어긋나는 일이 있었다.
마커가 붙은 '따라서' 문장과 답변의 첫 문장은 그대로 둔다."""
from __future__ import annotations

import re

_HEAD = "따라서"
_MARKER_RE = re.compile(r"\[[PN]\d+\]")
# 문장 머리: 앞 문장의 끝(마침표 + 공백) 또는 줄바꿈 바로 뒤
_BOUNDARY_RE = re.compile(r"(?:[.!?][\"'”’)]?\s+|\n\s*)$")
# 쥐고 있는 문장의 끝: 마침표 + 공백 뒤에 다음 글이 시작될 때(마침표 뒤에 붙는 마커는 그 문장의 것), 또는 줄바꿈
_END_RE = re.compile(r"[.!?][\"'”’)]?(\s+)(?=[^\s\[])|(?=\n)")


class ConclusionFilter:
    """feed(조각) · close() 가 내보낼 글을 돌려준다. '따라서'로 시작하는 문장만 끝날 때까지 쥐고, 나머지는 받은 대로 흘린다."""

    def __init__(self) -> None:
        self._pending = ""
        self._context = ""       # 이미 내보낸 글의 끝부분 — 문장 머리인지 볼 때 쓴다
        self._holding = False
        self.dropped: list[str] = []

    def feed(self, piece: str) -> str:
        self._pending += piece
        return self._drain(final=False)

    def close(self) -> str:
        return self._drain(final=True)

    def _emit(self, out: list[str], text: str) -> None:
        if text:
            out.append(text)
            self._context = (self._context + text)[-8:]

    def _drain(self, final: bool) -> str:
        out: list[str] = []
        while self._pending:
            if self._holding:
                m = _END_RE.search(self._pending)
                if not m and not final:
                    break
                if m and m.group(1) is not None:
                    cut, gap = m.start(1), m.group(1)
                else:
                    cut, gap = (m.start() if m else len(self._pending)), ""
                sentence, self._pending = self._pending[:cut], self._pending[cut + len(gap):]
                self._holding = False
                if _MARKER_RE.search(sentence):
                    self._emit(out, sentence + gap)
                else:
                    self.dropped.append(sentence.strip())
                    self._emit(out, "\n" * gap.count("\n"))   # 문단 나눔은 살린다
                continue
            start, wait = self._find_head(final)
            if start is None:
                self._emit(out, self._pending)
                self._pending = ""
                break
            self._emit(out, self._pending[:start])
            self._pending = self._pending[start:]
            if wait:
                break
            self._holding = True
        return "".join(out)

    def _find_head(self, final: bool) -> tuple[int | None, bool]:
        """문장 머리에 놓인 '따라서'의 위치. 조각이 '따' · '따라'에서 끊겼으면 (위치, 기다림)."""
        i = self._pending.find(_HEAD[0])
        while i != -1:
            rest = self._pending[i:i + len(_HEAD)]
            partial = len(rest) < len(_HEAD) and _HEAD.startswith(rest) and not final
            if (rest == _HEAD or partial) and _BOUNDARY_RE.search(self._context + self._pending[:i]):
                return i, partial
            i = self._pending.find(_HEAD[0], i + 1)
        return None, False
