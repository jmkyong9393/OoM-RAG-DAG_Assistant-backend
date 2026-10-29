package com.oom.assist.chat;

import com.oom.assist.common.context.TenantContext;
import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;

/**
 * search_sessions 기록. 스트림 시작 시 INSERT(status=streaming), done 가로채기에서 UPDATE(status=done), 실패 시 status=error.
 * 릴레이 스레드에서 호출되므로 트랜잭션 없이 자동 커밋으로 바로 반영된다.
 */
@Component
public class SessionRecorder {

  private final JdbcClient jdbc;

  public SessionRecorder(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  public long start(TenantContext ctx, String query, boolean policyEnabled) {
    return jdbc.sql("INSERT INTO search_sessions (workspace_id, user_id, query, mode, status) VALUES (?, ?, ?, ?, 'streaming') RETURNING id")
        .params(ctx.workspaceId(), ctx.userId(), query, policyEnabled ? "policy" : "dag_only").query(Long.class).single();
  }

  @SuppressWarnings("unchecked")
  public void done(long sessionId, Map<String, Object> done, String answerText, String policyJson, String chainJson) {
    Map<String, Object> latency = (Map<String, Object>) done.getOrDefault("latency", Map.of());
    jdbc.sql("""
        UPDATE search_sessions SET status = 'done', intent = ?, answer_text = ?, policy_refs = ?::jsonb, chain_ref = ?::jsonb,
          retrieved_ids = ?, verified_ids = ?, unverified_ids = ?,
          retrieval_ms = ?, first_token_ms = ?, total_ms = ?, model = ?, prompt_tokens = ?, completion_tokens = ?
        WHERE id = ?
        """).params(
            str(done.get("intent")), answerText, policyJson, chainJson,
            arr(done.get("retrieved_ids")), arr(done.get("verified_ids")), arr(done.get("unverified_ids")),
            num(latency.get("retrieval_ms")), num(latency.get("first_token_ms")), num(latency.get("total_ms")),
            str(done.get("model")), num(done.get("prompt_tokens")), num(done.get("completion_tokens")), sessionId)
        .update();
  }

  public void error(long sessionId, String answerSoFar) {
    jdbc.sql("UPDATE search_sessions SET status = 'error', answer_text = ? WHERE id = ? AND status = 'streaming'").params(answerSoFar, sessionId).update();
  }

  private static String str(Object o) { return o == null ? null : String.valueOf(o); }

  private static Integer num(Object o) { return o instanceof Number n ? n.intValue() : null; }

  private static String[] arr(Object o) {
    if (o instanceof List<?> l) return l.stream().map(String::valueOf).toArray(String[]::new);
    return new String[0];
  }
}
