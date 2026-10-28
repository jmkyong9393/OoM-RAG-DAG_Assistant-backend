package com.oom.assist.log;

import java.time.OffsetDateTime;
import java.util.List;

/** 프론트 계약 LogNode (BE-HANDOFF-001 §3-2). v_log_nodes 한 행과 1:1. JSON 은 snake_case 로 직렬화된다. */
public record LogNodeDto(
    long id,
    long workspaceId,
    String type,
    String status,
    String title,
    String body,
    String authorName,
    String pathLabel,
    String errorCode,
    List<String> keywords,
    String resolutionSummary,
    String refineStatus,
    OffsetDateTime lastConfirmedAt,
    Long deprecatedVia,
    String deprecateReason,
    OffsetDateTime resolvedAt,
    String resolveNote,
    Long rootId,
    Integer depth,
    String category,
    List<String> tags,
    OffsetDateTime createdAt) {}
