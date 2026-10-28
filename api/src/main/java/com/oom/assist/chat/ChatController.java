package com.oom.assist.chat;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
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
    return relay.stream(ctx, body.query().trim());
  }
}
