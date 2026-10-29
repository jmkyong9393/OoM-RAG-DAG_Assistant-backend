package com.oom.assist.log;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 업무 로그 규칙 (BE-HANDOFF-001 §2, 33 §6):
 *  R2  루트(부모 없음)는 fail/ref 만. success 루트는 problem_title 로 fail 루트를 만들고 그 아래 붙인 뒤 루트를 완료 처리한다.
 *  R3  상위가 다른 워크스페이스·폐기 상태면 등록 불가.
 *  R4  완료는 루트만. ref 루트는 대상 아님. 활성 success 자손이 없으면 note 필수.
 *  R5  폐기는 사유 필수. cascade 면 이 노드를 거치지 않고는 활성 조상에 닿을 수 없는 자손도 함께. 복원은 deprecated_via 로 연동.
 *  R12 success 의 부모는 fail/ref 만.
 * 간선 삽입은 워크스페이스 단위 advisory lock 으로 직렬화하고 would_create_cycle() 로 사이클을 막는다.
 */
@Service
public class LogService {

  private static final Set<String> TYPES = Set.of("fail", "success", "ref");

  private final LogRepository repo;

  public LogService(LogRepository repo) {
    this.repo = repo;
  }

  public record CreateRequest(String type, String title, String body, Long parentId, Boolean resolveRoot, String problemTitle, Integer composeMs) {}

  @Transactional
  public LogNodeDto create(TenantContext ctx, CreateRequest req) {
    String type = req.type();
    if (type == null || !TYPES.contains(type)) throw ApiException.badRequest("INVALID_TYPE", "type must be fail|success|ref");
    if (blank(req.title()) || blank(req.body())) throw ApiException.badRequest("VALIDATION", "title and body are required");
    long ws = ctx.workspaceId();
    repo.lockWorkspace(ws);

    Long parentId = req.parentId();
    if (parentId == null && "success".equals(type)) {
      // R2: success 루트 금지 → problem_title 로 fail 루트 자동 생성
      if (blank(req.problemTitle())) throw ApiException.badRequest("PROBLEM_TITLE_REQUIRED", "success without parent needs problem_title");
      long root = repo.insertNode(ws, "fail", req.problemTitle().trim(), req.problemTitle().trim(), ctx.userId(), null, "ui");
      long id = repo.insertNode(ws, "success", req.title().trim(), req.body().trim(), ctx.userId(), req.composeMs(), "ui");
      repo.insertEdge(ws, root, id, ctx.userId(), "manual");
      resolve(ctx, root, null, true);
      return repo.find(ws, id).orElseThrow();
    }

    if (parentId != null) {
      LogNodeDto parent = repo.find(ws, parentId).orElseThrow(() -> ApiException.conflict("PARENT_NOT_FOUND", "parent not in this workspace"));
      if (!"active".equals(parent.status())) throw ApiException.conflict("PARENT_DEPRECATED", "parent is deprecated");
      if ("success".equals(type) && !Set.of("fail", "ref").contains(parent.type()))
        throw ApiException.conflict("EDGE_PARENT_TYPE", "success can only follow fail/ref");
    }

    long id = repo.insertNode(ws, type, req.title().trim(), req.body().trim(), ctx.userId(), req.composeMs(), "ui");
    if (parentId != null) {
      repo.insertEdge(ws, parentId, id, ctx.userId(), "manual");
      if (Boolean.TRUE.equals(req.resolveRoot()) && "success".equals(type)) {
        Long root = repo.find(ws, id).map(LogNodeDto::rootId).orElse(null);
        if (root != null) resolve(ctx, root, null, true);
      }
    }
    return repo.find(ws, id).orElseThrow();
  }

  /** R4. note 는 활성 success 자손이 없을 때만 필수이며 그때만 저장한다. */
  @Transactional
  public LogNodeDto setResolved(TenantContext ctx, long id, boolean resolved, String note) {
    LogNodeDto node = get(ctx, id);
    if (!repo.isRoot(id)) throw ApiException.conflict("NOT_ROOT", "only a root (장애 건) can be resolved");
    if ("ref".equals(node.type())) throw ApiException.conflict("REF_ROOT", "ref root cannot be resolved");
    if (!resolved) {
      repo.setResolved(id, null, null);
      repo.audit(ctx.tenantId(), ctx.workspaceId(), "log_nodes", String.valueOf(id), "unresolved", ctx.userName(), null, node.title());
      return get(ctx, id);
    }
    boolean hasSuccess = repo.hasActiveSuccessDescendant(ctx.workspaceId(), id);
    if (!hasSuccess && blank(note)) throw ApiException.conflict("NOTE_REQUIRED", "no success log under this root; note required");
    repo.setResolved(id, OffsetDateTime.now(), hasSuccess ? null : note.trim());
    repo.audit(ctx.tenantId(), ctx.workspaceId(), "log_nodes", String.valueOf(id), "resolved", ctx.userName(), hasSuccess ? null : note.trim(), node.title());
    return get(ctx, id);
  }

  public record StatusResult(LogNodeDto node, List<Long> affected) {}

  /** R5. */
  @Transactional
  public StatusResult setStatus(TenantContext ctx, long id, String status, String reason, boolean cascade) {
    LogNodeDto node = get(ctx, id);
    List<Long> affected = new ArrayList<>();
    if ("deprecated".equals(status)) {
      if (blank(reason)) throw ApiException.badRequest("REASON_REQUIRED", "deprecate reason required");
      if (cascade) {
        affected = repo.orphanedDescendants(id);
        repo.setDeprecated(affected, reason.trim(), id);
      }
      repo.setDeprecated(List.of(id), reason.trim(), null);
      repo.audit(ctx.tenantId(), ctx.workspaceId(), "log_nodes", String.valueOf(id), "deprecated", ctx.userName(), reason.trim(), node.title());
    } else if ("active".equals(status)) {
      repo.restore(id);
      affected = repo.restoreCascade(id);
      repo.audit(ctx.tenantId(), ctx.workspaceId(), "log_nodes", String.valueOf(id), "restored", ctx.userName(), null, node.title());
    } else {
      throw ApiException.badRequest("INVALID_STATUS", "status must be active|deprecated");
    }
    return new StatusResult(get(ctx, id), affected);
  }

  @Transactional
  public LogNodeDto confirm(TenantContext ctx, long id) {
    get(ctx, id);
    repo.confirm(id);
    return get(ctx, id);
  }

  @Transactional
  public LogNodeDto requeueRefine(TenantContext ctx, long id) {
    get(ctx, id);
    repo.requeueRefine(id);
    return get(ctx, id);
  }

  public List<Long> orphaned(TenantContext ctx, long id) {
    get(ctx, id);
    return repo.orphanedDescendants(id);
  }

  @Transactional
  public void linkEdge(TenantContext ctx, long parentId, long childId) {
    long ws = ctx.workspaceId();
    repo.lockWorkspace(ws);
    LogNodeDto parent = repo.find(ws, parentId).orElseThrow(() -> ApiException.conflict("PARENT_NOT_FOUND", "parent not in this workspace"));
    LogNodeDto child = repo.find(ws, childId).orElseThrow(() -> ApiException.notFound("child"));
    if (!"active".equals(parent.status())) throw ApiException.conflict("PARENT_DEPRECATED", "parent is deprecated");
    if ("success".equals(child.type()) && !Set.of("fail", "ref").contains(parent.type()))
      throw ApiException.conflict("EDGE_PARENT_TYPE", "success can only follow fail/ref");
    if (repo.wouldCreateCycle(parentId, childId)) throw ApiException.conflict("CYCLE", "edge would create a cycle");
    repo.insertEdge(ws, parentId, childId, ctx.userId(), "manual");
  }

  @Transactional
  public void unlinkEdge(TenantContext ctx, long parentId, long childId) {
    if (repo.deleteEdge(ctx.workspaceId(), parentId, childId) == 0) throw ApiException.notFound("edge");
  }

  public LogNodeDto get(TenantContext ctx, long id) {
    return repo.find(ctx.workspaceId(), id).orElseThrow(() -> ApiException.notFound("log"));
  }

  private void resolve(TenantContext ctx, long root, String note, boolean resolved) {
    repo.setResolved(root, resolved ? OffsetDateTime.now() : null, note);
    repo.audit(ctx.tenantId(), ctx.workspaceId(), "log_nodes", String.valueOf(root), resolved ? "resolved" : "unresolved", ctx.userName(), note, null);
  }

  private static boolean blank(String s) {
    return s == null || s.isBlank();
  }
}
