package com.oom.assist.chat;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import com.oom.assist.common.util.Limits;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

@RestController
public class ChatController {

  private final ChatRelayService relay;

  public ChatController(ChatRelayService relay) {
    this.relay = relay;
  }

  public record ChatRequest(String query) {}

  @PostMapping("/chat")
  public SseEmitter chat(@RequestBody ChatRequest body, TenantContext ctx) {
    if (body.query() == null || body.query().isBlank()) throw ApiException.badRequest("VALIDATION", "query is required");
    Limits.max(body.query(), Limits.QUERY, "query");
    // 글자 · 숫자가 하나도 없는 질문("???")은 검색할 말이 없다 — 가까운 조문 아무거나를 근거로 답하게 두지 않는다
    if (body.query().codePoints().noneMatch(Character::isLetterOrDigit))
      throw ApiException.badRequest("VALIDATION", "query needs at least one letter or digit");
    return relay.stream(ctx, body.query().trim());
  }
}
