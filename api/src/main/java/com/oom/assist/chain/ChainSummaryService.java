package com.oom.assist.chain;

import com.oom.assist.common.config.AppProps;
import com.oom.assist.common.error.ApiException;
import com.oom.assist.common.util.Times;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.function.LongFunction;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import tools.jackson.databind.ObjectMapper;

/**
 * 트러블슈팅 'AI 요약' — 장애 건(루트~리프 전체)을 Python /ai/chain-summary 로 한 단락 요약하고 chain_summaries 에 저장한다.
 * GET /chains 가 저장된 요약이 없거나(none) 오래됐으면(stale) 백그라운드로 summarize 를 시작하고(view) 응답은 기다리지 않는다 —
 * 처음엔 틀 문장 · 이전 요약, 다음 조회부터 새 요약. LLM 은 지문(요약에 영향을 주는 값의 md5)이 바뀌었을 때만 부른다. 처리 순서:
 * 폐기 → 지문 같음(fresh) → 같은 지문 실패 3회 · 5분 이내 실패 → 생성 중 표시(선점) 실패(generating) → Python 호출.
 * LLM 호출은 트랜잭션 밖에서 한다(선점 · 저장만 각각 짧게) — 최대 20초 동안 DB 연결을 잡지 않게.
 */
@Service
public class ChainSummaryService {

  // 순서 유지: Python 요약 제한(15초, ai/app/main.py CHAIN_SUMMARY_TIMEOUT_SEC) < AI_TIMEOUT(20초) < CLAIM_STALE_SEC(30초)
  static final Duration AI_TIMEOUT = Duration.ofSeconds(20);
  static final int CLAIM_STALE_SEC = 30;   // 생성 중 표시가 이보다 오래면 요약 도중 죽은 것으로 보고 다시 선점
  // 같은 지문으로 형식 오류(AI_BAD_OUTPUT — LLM 이 답해 비용이 든 실패)가 3번이면 지문이 바뀔 때까지 다시 시도하지 않는다.
  // 서버 꺼짐 · 시간 초과 · 429(사용량 · 크레딧) · 기타 오류는 비용이 들지 않아 세지 않고 5분 대기만 한다 — 장애가 길어도 끝나면 다시 요약된다.
  static final int MAX_FAILS = 3;
  static final int FAIL_WAIT_MIN = 5;      // 실패 후 대기 — 일시 장애 동안 여는 사람마다 20초씩 기다리지 않게
  public static final int INPUT_NODES = 40;  // 요약 입력 기록 수 상한 — 넘으면 해결 · 원인 경로를 먼저 남긴다(get_chain). 500자 × 40 ≈ 2만 자
  static final int BODY_MAX = 500;         // 요약 입력에 넣는 기록 본문 길이 상한(자) — 긴 장애 건의 입력이 커지지 않게
  // 요약 규칙 버전 — Python 프롬프트(ai/app/llm/client.py CHAIN_SUMMARY_SYSTEM)나 입력 형식(_chain_tree · _chain_header)을 바꾸면 올린다.
  // 지문 앞에 붙으므로 올리면 기록이 그대로여도 모든 장애 건이 다음에 열릴 때 한 번씩 새 규칙으로 다시 요약된다(그동안은 stale).
  static final String SUMMARY_VERSION = "1";

  static final Map<String, String> MESSAGES = Map.of(
      "AI_TIMEOUT", "AI 서버가 응답하지 않습니다",
      "AI_RATE_LIMIT", "AI 사용량이 많아 잠시 후 다시 시도합니다",
      "AI_BAD_OUTPUT", "요약 결과가 올바르지 않습니다",
      "AI_ERROR", "요약 생성 중 오류가 발생했습니다",
      "AI_DISABLED", "AI 요약이 꺼져 있습니다");

  /**
   * status: fresh | stale(이전 요약 — 최근 변경 미반영) | failed(보여줄 요약 없음) | generating | deprecated.
   * summary 는 LLM 요약만 담는다(없으면 null — 한 줄 문장은 프론트가 GET /chains 로 이미 가진 것을 쓴다).
   * retryable=false 면 같은 내용으로 3회 실패해 기록이 바뀔 때까지 다시 시도하지 않는다.
   */
  public record Result(String status, String summary, OffsetDateTime generatedAt, Integer basedOnRecords, int currentRecords,
                       String code, String message, boolean retryable) {}

  private static final Logger log = LoggerFactory.getLogger(ChainSummaryService.class);

  private final JdbcClient jdbc;
  private final AppProps props;
  private final ObjectMapper json;
  private final Background background;
  // HTTP/1.1 고정 — ChatRelayService 와 같은 이유(uvicorn 이 h2c 업그레이드 요청의 본문을 버린다)
  private final HttpClient http = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).connectTimeout(Duration.ofSeconds(5)).build();

  public ChainSummaryService(JdbcClient jdbc, AppProps props, ObjectMapper json, Background background) {
    this.jdbc = jdbc;
    this.props = props;
    this.json = json;
    this.background = background;
  }

  /**
   * 백그라운드 요약 실행 — 가상 스레드. Executor 빈으로 두지 않는다(그러면 Spring Boot 기본 작업 실행기가 빠진다).
   * 테스트는 같은 스레드에서 돌게 바꾼다(테스트 트랜잭션의 데이터를 보도록).
   */
  public interface Background {
    void run(Runnable task);
  }

  @Configuration(proxyBeanMethods = false)
  static class BackgroundConfig {
    @Bean
    Background chainSummaryBackground() {
      return Executors.newVirtualThreadPerTaskExecutor()::execute;
    }
  }

  /** GET /chains 용 — 저장된 AI 요약과 그 상태. LLM 을 부르지 않고 저장값 · 지문만 본다. */
  public record View(String status, String summary, OffsetDateTime at) {}

  /** status: fresh(최신) | stale(이전 요약) | generating(다른 요청이 만드는 중) | none(요약 없음) | deprecated. */
  public View peek(long ws, long root) {
    String rootStatus = jdbc.sql("SELECT status FROM log_nodes WHERE id = ?").param(root).query(String.class).single();
    Map<String, Object> row = stored(root);
    String summary = row == null ? null : (String) row.get("summary");
    OffsetDateTime at = summary == null ? null : Times.offset(row.get("created_at"));
    if ("deprecated".equals(rootStatus)) return new View("deprecated", summary, at);
    if (summary != null && signature(ws, root).equals(row.get("summary_sig"))) return new View("fresh", summary, at);
    if (row != null && row.get("generating_since") != null
        && Times.offset(row.get("generating_since")).isAfter(dbNow().minusSeconds(CLAIM_STALE_SEC))) return new View("generating", summary, at);
    return new View(summary != null ? "stale" : "none", summary, at);
  }

  /**
   * GET /chains 용 — peek 결과를 그대로 돌려주고, 요약이 없거나(none) 오래됐으면(stale) 백그라운드로 다시 만든다.
   * 생성 중 · 폐기 · 최신이면 시작하지 않는다. 실패 대기 · 3회 중단 · 선점은 summarize 가 그대로 지킨다.
   */
  public View view(long ws, long root, LongFunction<ChainController.Chain> chainOf) {
    View v = peek(ws, root);
    if ("none".equals(v.status()) || "stale".equals(v.status())) {
      background.run(() -> {
        try {
          summarize(ws, root, chainOf);
        } catch (RuntimeException e) {   // 요약은 보조 기능 — 다음 조회에 다시 시도한다
          log.warn("chain summary for root {} failed: {}", root, e.toString());
        }
      });
    }
    return v;
  }

  /** chainOf: 루트 id → 장애 건 요약 입력 체인(최대 INPUT_NODES 건, total = 전체 수). */
  public Result summarize(long ws, long entryId, LongFunction<ChainController.Chain> chainOf) {
    Long root = jdbc.sql("SELECT root_id FROM v_log_nodes WHERE workspace_id = ? AND id = ?").params(ws, entryId)
        .query(Long.class).optional().orElseThrow(() -> ApiException.notFound("chain"));
    String rootStatus = jdbc.sql("SELECT status FROM log_nodes WHERE id = ?").param(root).query(String.class).single();
    // 기록 수는 타임라인 카드 '기록 N건'과 같은 기준(폐기 제외). 폐기 여부는 지문에 들어가 있어 폐기해도 다시 요약된다
    int records = jdbc.sql("SELECT count(*) FROM bfs_levels(?, ARRAY[?]::bigint[], true) g JOIN log_nodes n ON n.id = g.node_id WHERE n.status = 'active'")
        .params(ws, root).query(Integer.class).single();
    Map<String, Object> row = stored(root);

    if ("deprecated".equals(rootStatus)) return result("deprecated", row, records, null, false);   // 폐기된 장애 건은 LLM 을 부르지 않는다

    String sig = signature(ws, root);
    if (row != null && row.get("summary") != null && sig.equals(row.get("summary_sig"))) return result("fresh", row, records, null, false);

    boolean sameSig = row != null && sig.equals(row.get("sig"));
    if (sameSig && ((Number) row.get("fail_count")).intValue() >= MAX_FAILS) return failed(row, records, false);
    if (sameSig && row.get("failed_at") != null
        && Times.offset(row.get("failed_at")).isAfter(dbNow().minusMinutes(FAIL_WAIT_MIN))) return failed(row, records, true);
    if (!claim(root, sig)) return result("generating", row, records, null, true);

    String code;
    String error = null;
    String text = null;
    Map<?, ?> usage = Map.of();
    try {
      Map<?, ?> res = callAi(payload(chainOf.apply(root)));
      code = (String) res.get("code");
      text = code == null ? (String) res.get("summary") : null;
      usage = res;
      if (code == null && (text == null || text.isBlank())) code = "AI_BAD_OUTPUT";
    } catch (java.net.http.HttpTimeoutException | java.net.ConnectException e) {
      code = "AI_TIMEOUT";
      error = e.toString();
    } catch (Exception e) {   // Python 5xx · 응답 형식 오류 등 — 요약은 보조 기능이라 종류만 남기고 이전 요약으로 답한다
      code = "AI_ERROR";
      error = e.toString();
    }

    if (code == null) {
      jdbc.sql("""
          UPDATE chain_summaries SET summary = ?, summary_sig = ?, summary_records = ?, created_at = now(),
                 model = ?, prompt_tokens = ?, completion_tokens = ?,
                 fail_count = 0, error_kind = NULL, last_error = NULL, failed_at = NULL, generating_since = NULL
          WHERE root_id = ?""").params(text, sig, records,
              usage.get("model"), intOrNull(usage.get("prompt_tokens")), intOrNull(usage.get("completion_tokens")), root)
          .update();
      return result("fresh", stored(root), records, null, false);
    }
    if ("AI_DISABLED".equals(code)) {   // 키 없음 · fake — 실패로 세지 않는다
      jdbc.sql("UPDATE chain_summaries SET generating_since = NULL WHERE root_id = ?").param(root).update();
      Map<String, Object> now = stored(root);
      return result(now.get("summary") != null ? "stale" : "failed", now, records, code, true);
    }
    jdbc.sql("""
        UPDATE chain_summaries SET fail_count = fail_count + CASE WHEN ? = 'AI_BAD_OUTPUT' THEN 1 ELSE 0 END,
               error_kind = ?, last_error = ?, failed_at = now(), generating_since = NULL
        WHERE root_id = ?""").params(code, code, error == null ? code : error, root).update();   // 비용이 든 형식 오류만 센다(MAX_FAILS)
    Map<String, Object> now = stored(root);
    return failed(now, records, ((Number) now.get("fail_count")).intValue() < MAX_FAILS);
  }

  /**
   * 지문 — 요약 입력에 들어가는 값만: 하위 트리 노드(유형 · 상태 · 원인 표시 · 제목 · 본문), 간선(연결 사유), 해결 기록.
   * 장애 건 완료 처리 여부는 넣지 않는다 — 요약은 내용만 쓰고 상태는 화면이 표시하므로 완료 처리만으로는 다시 요약할 필요가 없다.
   * updated_at 은 쓰지 않는다(토큰 · 임베딩 백필처럼 요약과 무관한 UPDATE 에도 바뀐다). AI 정리 결과(resolution_summary)는 요약 입력이 아니라 넣지 않는다
   * — 그래서 새 기록의 AI 정리를 기다릴 필요도, 정리가 끝났다고 다시 요약할 필요도 없다.
   */
  String signature(long ws, long root) {
    return SUMMARY_VERSION + ":" + jdbc.sql("""
        WITH t AS (SELECT node_id FROM bfs_levels(:ws, ARRAY[:root]::bigint[], true))
        SELECT md5(
          coalesce((SELECT string_agg(concat_ws('|', n.id, n.type, n.status, n.category, n.title, n.body), E'\\n' ORDER BY n.id)
                    FROM log_nodes n WHERE n.id IN (SELECT node_id FROM t)), '')
          || '#' || coalesce((SELECT string_agg(concat_ws('|', e.parent_id, e.child_id, e.reason), E'\\n' ORDER BY e.parent_id, e.child_id)
                    FROM log_edges e WHERE e.parent_id IN (SELECT node_id FROM t)), '')
          || '#' || coalesce((SELECT string_agg(x::text, ',' ORDER BY x) FROM resolved_nodes(:ws, :root) AS x), ''))
        """).param("ws", ws).param("root", root).query(String.class).single();
  }

  /** 생성 중 표시를 원자적으로 건다. 지문이 바뀌었으면 여기서 실패 횟수를 0 으로 — 실패 저장은 단순히 +1 만 한다. */
  private boolean claim(long root, String sig) {
    return jdbc.sql("""
        INSERT INTO chain_summaries (root_id, sig, generating_since) VALUES (?, ?, now())
        ON CONFLICT (root_id) DO UPDATE
          SET generating_since = now(),
              fail_count = CASE WHEN chain_summaries.sig = EXCLUDED.sig THEN chain_summaries.fail_count ELSE 0 END,
              sig = EXCLUDED.sig
          WHERE chain_summaries.generating_since IS NULL OR chain_summaries.generating_since < now() - make_interval(secs => ?)
        RETURNING root_id
        """).params(root, sig, CLAIM_STALE_SEC).query(Long.class).optional().isPresent();
  }

  /**
   * Python 이 읽는 키 이름으로 직접 맞춘다. 기록 내용은 사람이 쓴 본문(앞 BODY_MAX 자)을 보낸다 — AI 정리 문장(note)을 다시 요약하면
   * 정리에서 빠진 내용은 요약에도 없고 정리가 덧붙인 말('해결했습니다')은 그대로 남았다. 날짜는 회사 기준(한국) 날짜.
   */
  private Map<String, Object> payload(ChainController.Chain chain) {
    List<Long> ids = chain.nodes().stream().map(ChainController.ChainNode::id).toList();
    Map<Long, Map<String, Object>> extra = new HashMap<>();
    jdbc.sql("""
        SELECT id, to_char(created_at AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD') AS created, left(body, ?) AS body
        FROM log_nodes WHERE id = ANY (?)""").params(BODY_MAX, ids.toArray(Long[]::new)).query().listOfRows()
        .forEach(r -> extra.put(((Number) r.get("id")).longValue(), r));
    List<Map<String, Object>> nodes = new ArrayList<>();
    for (ChainController.ChainNode n : chain.nodes()) {
      Map<String, Object> m = new HashMap<>();
      m.put("id", n.id());
      m.put("type", n.type());
      m.put("status", n.status());
      m.put("title", n.title());
      m.put("is_cause", n.isCause());
      m.put("resolves_root", n.resolvesRoot());
      Map<String, Object> x = extra.get(n.id());
      if (x != null) {
        m.put("created_at", x.get("created"));
        m.put("body", x.get("body"));
      }
      nodes.add(m);
    }
    List<Map<String, Object>> edges = chain.edges().stream().map(e -> {
      Map<String, Object> m = new HashMap<>();
      m.put("parent_id", e.parentId());
      m.put("child_id", e.childId());
      m.put("reason", e.reason());
      return m;
    }).toList();
    return Map.of("nodes", nodes, "edges", edges, "total", chain.total());
  }

  private Map<?, ?> callAi(Map<String, Object> body) throws Exception {
    HttpRequest req = HttpRequest.newBuilder(URI.create(props.ai().baseUrl() + "/ai/chain-summary")).timeout(AI_TIMEOUT)
        .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(body))).build();
    HttpResponse<String> res = http.send(req, HttpResponse.BodyHandlers.ofString());
    if (res.statusCode() / 100 != 2) throw new IllegalStateException("ai service responded " + res.statusCode());
    return json.readValue(res.body(), Map.class);
  }

  private Map<String, Object> stored(long root) {
    return jdbc.sql("SELECT * FROM chain_summaries WHERE root_id = ?").param(root).query().listOfRows().stream().findFirst().orElse(null);
  }

  private static Integer intOrNull(Object v) {
    return v instanceof Number n ? n.intValue() : null;
  }

  private OffsetDateTime dbNow() {
    return Times.offset(jdbc.sql("SELECT now()").query(java.sql.Timestamp.class).single());
  }

  /** 새 요약을 못 만들었을 때: 이전 요약이 있으면 stale, 없으면 failed. */
  private Result failed(Map<String, Object> row, int records, boolean retryable) {
    boolean hasSummary = row != null && row.get("summary") != null;
    return result(hasSummary ? "stale" : "failed", row, records, row == null ? null : (String) row.get("error_kind"), retryable);
  }

  private Result result(String status, Map<String, Object> row, int records, String code, boolean retryable) {
    String summary = row == null ? null : (String) row.get("summary");
    OffsetDateTime at = summary == null ? null : Times.offset(row.get("created_at"));
    Integer based = summary == null || row.get("summary_records") == null ? null : ((Number) row.get("summary_records")).intValue();
    return new Result(status, summary, at, based, records, code, code == null ? null : MESSAGES.get(code), retryable);
  }
}
