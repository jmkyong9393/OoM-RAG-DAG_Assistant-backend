package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.oom.assist.common.context.TenantContextFilter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.web.servlet.request.MockMultipartHttpServletRequestBuilder;

/** 규정 업로드: 저장 · 검증 · 권한, 검수(변환본 수정 · 승인 · 반려 · 다시 변환), 원본 · 변환본 내려받기. 워커 몫(변환 · 등록)은 상태만 흉내 낸다. */
class PolicyUploadApiTest extends AbstractApiTest {

  static final byte[] MD = "제1조(목적) 이 규정은 시험용이다.\n제2조(적용) 모든 직원에게 적용한다.\n".getBytes(StandardCharsets.UTF_8);
  @Value("${oom.storage.uploads-dir}") String uploadsDir;
  final List<Path> created = new ArrayList<>();

  @AfterEach
  void cleanFiles() throws Exception {
    for (Path p : created) {
      Files.deleteIfExists(p);
      Files.deleteIfExists(p.getParent());
    }
  }

  @Test
  void uploadStoresOriginalWithHashAndQueuesConversion() throws Exception {
    String body = mvc.perform(upload(userBackend, wsHr, "규정.md", MD, "test-rule-v1", "dept"))
        .andExpect(status().isAccepted())
        .andExpect(jsonPath("$.status").value("converting"))
        .andExpect(jsonPath("$.format").value("md"))
        .andExpect(jsonPath("$.original_name").value("규정.md"))
        .andExpect(jsonPath("$.workspace_id").value(wsHr))
        .andExpect(jsonPath("$.uploaded_by").value("장문경"))
        .andExpect(jsonPath("$.can_manage").value(true))
        .andReturn().getResponse().getContentAsString();
    long id = com.jayway.jsonpath.JsonPath.parse(body).read("$.id", Long.class);
    Path stored = stored(id);
    assertThat(Files.readAllBytes(stored)).isEqualTo(MD);
    assertThat(com.jayway.jsonpath.JsonPath.parse(body).read("$.sha256", String.class))
        .isEqualTo(HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(MD)));
    mvc.perform(as(get("/documents/uploads"), userBackend, wsHr)).andExpect(jsonPath("$", hasSize(1))).andExpect(jsonPath("$[0].markdown").isEmpty());
    mvc.perform(as(get("/documents/uploads"), userLead, wsSw)).andExpect(jsonPath("$", hasSize(0)));   // 다른 부서 업로드는 안 보인다
    mvc.perform(as(get("/documents/uploads/" + id + "/original"), userBackend, wsHr)).andExpect(status().isOk())
        .andExpect(content().bytes(MD)).andExpect(header().string("Content-Disposition", org.hamcrest.Matchers.containsString("UTF-8''")));
  }

  @Test
  void uploadValidatesFormatSlugTargetAndPermission() throws Exception {
    mvc.perform(upload(userBackend, wsHr, "규정.exe", MD, "test-rule-v1", "dept")).andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("UNSUPPORTED_FORMAT"));
    mvc.perform(upload(userBackend, wsHr, "규정.md", MD, "Bad Slug", "dept")).andExpect(status().isBadRequest());
    jdbc.sql("INSERT INTO policy_documents (workspace_id, slug, title, version, source_type, status) VALUES (?, 'taken-v1', 'T', '1', 'markdown', 'active')").param(wsHr).update();
    mvc.perform(upload(userBackend, wsHr, "규정.md", MD, "taken-v1", "dept")).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SLUG_TAKEN"));
    mvc.perform(upload(userBackend, wsHr, "규정.md", MD, "test-rule-v2", "dept").param("supersedes", "no-such")).andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("SUPERSEDES_NOT_FOUND"));
    mvc.perform(upload(userBackend, wsSw, "규정.md", MD, "test-rule-v1", "dept")).andExpect(status().isForbidden());     // SW 개발에선 member
    mvc.perform(upload(userBackend, wsHr, "규정.md", MD, "test-rule-v1", "company")).andExpect(status().isForbidden()); // 전사 공통은 회사 관리자만
    long boss = user("관리자", "팀장", "T0009", "boss@test.local");
    member(company, boss, "admin");
    member(wsHr, boss, "member");
    String body = mvc.perform(upload(boss, wsHr, "규정.md", MD, "test-rule-v1", "company")).andExpect(status().isAccepted())
        .andExpect(jsonPath("$.workspace_id").value(company)).andReturn().getResponse().getContentAsString();
    stored(com.jayway.jsonpath.JsonPath.parse(body).read("$.id", Long.class));
  }

  @Test
  void reviewEditApproveRejectAndRetry() throws Exception {
    long id = uploaded("test-rule-v1");
    mvc.perform(as(post("/documents/uploads/" + id + "/approve"), userBackend, wsHr)).andExpect(status().isConflict())
        .andExpect(jsonPath("$.code").value("NOT_IN_REVIEW"));   // 변환 중에는 승인 불가
    workerConverted(id, 0);
    mvc.perform(as(post("/documents/uploads/" + id + "/approve"), userBackend, wsHr)).andExpect(status().isConflict())
        .andExpect(jsonPath("$.code").value("NO_ARTICLES"));
    // 검수자가 변환본을 고치면 미리보기를 다시 만들도록 converting 으로
    mvc.perform(as(put("/documents/uploads/" + id + "/markdown"), userBackend, wsHr).content("{\"markdown\":\"제1조(목적) 고친 본문\"}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("converting")).andExpect(jsonPath("$.markdown").value("제1조(목적) 고친 본문"));
    workerConverted(id, 2);
    mvc.perform(as(post("/documents/uploads/" + id + "/approve"), userLead, wsSw)).andExpect(status().isNotFound());   // 다른 부서 사람에게는 없는 업로드
    mvc.perform(as(post("/documents/uploads/" + id + "/approve"), userBackend, wsHr)).andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("indexing")).andExpect(jsonPath("$.reviewed_by").value("장문경"));
    mvc.perform(as(get("/documents/uploads/" + id + "/markdown"), userBackend, wsHr)).andExpect(status().isOk())
        .andExpect(content().string("제1조(목적) 고친 본문"));

    long other = uploaded("test-rule-v9");
    jdbc.sql("UPDATE policy_uploads SET status = 'failed', error = 'ocr crashed' WHERE id = ?").param(other).update();
    mvc.perform(as(post("/documents/uploads/" + other + "/retry"), userBackend, wsHr)).andExpect(jsonPath("$.status").value("converting"))
        .andExpect(jsonPath("$.error").isEmpty());
    workerConverted(other, 1);
    mvc.perform(as(post("/documents/uploads/" + other + "/reject"), userBackend, wsHr).content("{}")).andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("REASON_REQUIRED"));
    mvc.perform(as(post("/documents/uploads/" + other + "/reject"), userBackend, wsHr).content("{\"reason\":\"스캔이 흐림\"}"))
        .andExpect(jsonPath("$.status").value("rejected")).andExpect(jsonPath("$.reject_reason").value("스캔이 흐림"));
    // 반려된 slug 는 다시 쓸 수 있다
    mvc.perform(upload(userBackend, wsHr, "규정.md", MD, "test-rule-v9", "dept")).andExpect(status().isAccepted());
    jdbc.sql("SELECT original_path FROM policy_uploads WHERE slug = 'test-rule-v9' AND status = 'converting'").query(String.class).list()
        .forEach(rel -> created.add(Path.of(uploadsDir).toAbsolutePath().normalize().resolve(rel)));
    assertThat(jdbc.sql("SELECT action FROM audit_log WHERE table_name = 'policy_uploads' AND row_id = ? ORDER BY id").param(String.valueOf(id)).query(String.class).list())
        .containsExactly("uploaded", "edited", "approved");
  }

  @Test
  void slugIsFreedWhenRegisteredDocumentIsPurged() throws Exception {
    long id = uploaded("test-rule-purge");
    mvc.perform(upload(userBackend, wsHr, "규정.md", MD, "test-rule-purge", "dept")).andExpect(status().isConflict())
        .andExpect(jsonPath("$.code").value("SLUG_TAKEN"));   // 진행 중인 업로드
    // 워커가 등록을 끝낸 상태: 문서가 생기고 업로드는 approved
    long doc = jdbc.sql("INSERT INTO policy_documents (workspace_id, slug, title, version, source_type, status) VALUES (?, 'test-rule-purge', '시험 규정', '2026-11', 'markdown', 'active') RETURNING id")
        .param(wsHr).query(Long.class).single();
    jdbc.sql("UPDATE policy_uploads SET status = 'approved', document_id = ? WHERE id = ?").params(doc, id).update();
    mvc.perform(upload(userBackend, wsHr, "규정.md", MD, "test-rule-purge", "dept")).andExpect(status().isConflict());   // 문서가 있는 동안
    mvc.perform(as(patch("/documents/" + doc), userBackend, wsHr).content("{\"status\":\"retired\",\"reason\":\"잘못 올림\"}")).andExpect(status().isOk());
    mvc.perform(as(delete("/documents/" + doc), userBackend, wsHr)).andExpect(status().isNoContent());
    mvc.perform(upload(userBackend, wsHr, "규정.md", MD, "test-rule-purge", "dept")).andExpect(status().isAccepted());   // 영구 삭제 뒤에는 다시 쓸 수 있다
    jdbc.sql("SELECT original_path FROM policy_uploads WHERE slug = 'test-rule-purge' AND status = 'converting'").query(String.class).list()
        .forEach(rel -> created.add(Path.of(uploadsDir).toAbsolutePath().normalize().resolve(rel)));
  }

  private long uploaded(String slug) throws Exception {
    String body = mvc.perform(upload(userBackend, wsHr, "규정.md", MD, slug, "dept")).andExpect(status().isAccepted()).andReturn().getResponse().getContentAsString();
    long id = com.jayway.jsonpath.JsonPath.parse(body).read("$.id", Long.class);
    stored(id);
    return id;
  }

  /** 워커가 변환을 끝낸 상태(review)와 조문 수를 흉내 낸다. */
  private void workerConverted(long id, int articles) {
    jdbc.sql("UPDATE policy_uploads SET status = 'review', markdown = coalesce(markdown, '제1조(목적) 변환본'), report = jsonb_build_object('article_count', ?) WHERE id = ?")
        .params(articles, id).update();
  }

  private Path stored(long id) {
    String rel = jdbc.sql("SELECT original_path FROM policy_uploads WHERE id = ?").param(id).query(String.class).single();
    Path p = Path.of(uploadsDir).toAbsolutePath().normalize().resolve(rel);
    created.add(p);
    return p;
  }

  private MockMultipartHttpServletRequestBuilder upload(long userId, long ws, String filename, byte[] bytes, String slug, String scope) {
    MockMultipartHttpServletRequestBuilder b = multipart("/documents/uploads").file(new MockMultipartFile("file", filename, "application/octet-stream", bytes));
    b.param("title", "시험 규정").param("version", "2026-11").param("slug", slug).param("scope", scope);
    b.cookie(loginCookie(userId)).header(TenantContextFilter.WORKSPACE_HEADER, ws);
    return b;
  }
}
