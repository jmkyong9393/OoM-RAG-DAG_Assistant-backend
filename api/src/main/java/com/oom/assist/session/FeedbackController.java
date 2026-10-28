package com.oom.assist.session;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
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
  public ResponseEntity<Void> feedback(@RequestBody FeedbackRequest body, TenantContext ctx) {
    boolean exists = jdbc.sql("SELECT EXISTS (SELECT 1 FROM search_sessions WHERE id = ? AND workspace_id = ?)")
        .params(body.sessionId(), ctx.workspaceId()).query(Boolean.class).single();
    if (!exists) throw ApiException.notFound("session");
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
