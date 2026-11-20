package com.oom.assist.session;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import com.oom.assist.common.util.Limits;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/** 답변 피드백. 세션당 1개(UNIQUE). score null 은 철회(DELETE). 대시보드 만족률의 원천. */
@RestController
public class FeedbackController {

  private final JdbcClient jdbc;

  public FeedbackController(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  public record FeedbackRequest(@NotNull Long sessionId, Integer score, String comment) {}

  @PostMapping("/feedback")
  @Transactional
  public ResponseEntity<Void> feedback(@Valid @RequestBody FeedbackRequest body, TenantContext ctx) {
    var found = jdbc.sql("SELECT user_id FROM search_sessions WHERE id = ? AND workspace_id = ?")
        .params(body.sessionId(), ctx.workspaceId()).query().listOfRows();
    if (found.isEmpty()) throw ApiException.notFound("session");
    // 만족 응답은 질문 하나에 하나뿐이라, 질문한 사람만 남기고 바꾸고 지운다
    if (!(found.get(0).get("user_id") instanceof Number owner) || owner.longValue() != ctx.userId())
      throw ApiException.forbidden("only the person who asked can rate this answer");
    Limits.max(body.comment(), Limits.COMMENT, "comment");
    if (body.score() == null) {
      jdbc.sql("DELETE FROM answer_feedback WHERE session_id = ?").param(body.sessionId()).update();
    } else {
      if (body.score() != 1 && body.score() != -1) throw ApiException.badRequest("VALIDATION", "score must be 1, -1 or null");
      jdbc.sql("""
          INSERT INTO answer_feedback (session_id, score, comment) VALUES (?, ?, ?)
          ON CONFLICT (session_id) DO UPDATE SET score = EXCLUDED.score, comment = EXCLUDED.comment, updated_at = now()
          """).params(body.sessionId(), body.score(), body.comment()).update();
    }
    return ResponseEntity.noContent().build();
  }
}
