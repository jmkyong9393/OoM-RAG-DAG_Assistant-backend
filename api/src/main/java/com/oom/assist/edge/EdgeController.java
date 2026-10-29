package com.oom.assist.edge;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.log.LogService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/** 기존 노드 간 연결·해제 (타임라인 노드 상세). 등록 시 상위 선택은 POST /logs 가 처리한다. */
@RestController
public class EdgeController {

  private final LogService service;

  public EdgeController(LogService service) {
    this.service = service;
  }

  public record EdgeRequest(@NotNull Long parentId, @NotNull Long childId) {}

  @PostMapping("/edges")
  public ResponseEntity<Void> link(@Valid @RequestBody EdgeRequest body, TenantContext ctx) {
    service.linkEdge(ctx, body.parentId(), body.childId());
    return ResponseEntity.status(201).build();
  }

  @DeleteMapping("/edges")
  public ResponseEntity<Void> unlink(@Valid @RequestBody EdgeRequest body, TenantContext ctx) {
    service.unlinkEdge(ctx, body.parentId(), body.childId());
    return ResponseEntity.noContent().build();
  }
}
