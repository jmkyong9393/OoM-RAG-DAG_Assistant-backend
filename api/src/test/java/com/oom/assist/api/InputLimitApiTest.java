package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.oom.assist.common.util.Limits;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockMultipartFile;

/** 입력 길이 상한, 뜻 없는 질문, 만족 응답의 주인, 읽을 수 없는 본문의 응답 모양. */
class InputLimitApiTest extends AbstractApiTest {

  private static String json(String key, String value) {
    return "\"" + key + "\":\"" + value + "\"";
  }

  @Test
  void logTitleBodyAndReasonsAreCapped() throws Exception {
    String longTitle = "가".repeat(Limits.LOG_TITLE + 1), longBody = "가".repeat(Limits.LOG_BODY + 1);
    long before = jdbc.sql("SELECT count(*) FROM log_nodes").query(Long.class).single();
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"fail\"," + json("title", longTitle) + "," + json("body", "b") + "}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"fail\"," + json("title", "t") + "," + json("body", longBody) + "}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    mvc.perform(as(post("/logs"), userBackend, wsSw)
            .content("{\"type\":\"success\"," + json("title", "t") + "," + json("body", "b") + "," + json("problem_title", longTitle) + "}"))
        .andExpect(status().isBadRequest());
    mvc.perform(as(post("/logs/bulk"), userLead, wsSw)
            .content("{\"items\":[{\"type\":\"fail\"," + json("title", longTitle) + "," + json("body", "b") + "}]}"))
        .andExpect(status().isBadRequest());
    assertThat(jdbc.sql("SELECT count(*) FROM log_nodes").query(Long.class).single()).isEqualTo(before);

    // 상한에 딱 맞는 글은 들어간다
    String ok = mvc.perform(as(post("/logs"), userBackend, wsSw)
            .content("{\"type\":\"fail\"," + json("title", "가".repeat(Limits.LOG_TITLE)) + "," + json("body", "가".repeat(Limits.LOG_BODY)) + "}"))
        .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
    int id = com.jayway.jsonpath.JsonPath.read(ok, "$.id");
    mvc.perform(as(patch("/logs/" + id), userBackend, wsSw).content("{\"resolved\":true," + json("note", "가".repeat(Limits.NOTE + 1)) + "}"))
        .andExpect(status().isBadRequest());
    mvc.perform(as(patch("/logs/" + id), userBackend, wsSw).content("{\"status\":\"deprecated\"," + json("reason", "가".repeat(Limits.NOTE + 1)) + "}"))
        .andExpect(status().isBadRequest());
  }

  @Test
  void chatRejectsTooLongAndWordlessQueries() throws Exception {
    long before = jdbc.sql("SELECT count(*) FROM search_sessions").query(Long.class).single();
    mvc.perform(as(post("/chat"), userBackend, wsSw).content("{" + json("query", "가".repeat(Limits.QUERY + 1)) + "}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    for (String q : new String[] {"???", "!!! ... ~~", "🙂🙂"})
      mvc.perform(as(post("/chat"), userBackend, wsSw).content("{" + json("query", q) + "}"))
          .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    assertThat(jdbc.sql("SELECT count(*) FROM search_sessions").query(Long.class).single()).isEqualTo(before);
  }

  @Test
  void onlyTheAskerRatesAnAnswer() throws Exception {
    long s = jdbc.sql("INSERT INTO search_sessions (workspace_id, user_id, query, mode, status) VALUES (?, ?, 'q', 'dag_only', 'done') RETURNING id")
        .params(wsSw, userBackend).query(Long.class).single();
    mvc.perform(as(post("/feedback"), userBackend, wsSw).content("{\"session_id\":" + s + ",\"score\":1}")).andExpect(status().isNoContent());
    // 질문 기록은 내 질문인지 알려 준다 — 화면이 남의 질문에서 만족 버튼을 끈다
    mvc.perform(as(get("/sessions/" + s), userBackend, wsSw)).andExpect(jsonPath("$.mine").value(true));
    mvc.perform(as(get("/sessions/" + s), userLead, wsSw)).andExpect(jsonPath("$.mine").value(false));
    // 같은 부서의 다른 사람은 바꾸지도 지우지도 못한다
    mvc.perform(as(post("/feedback"), userLead, wsSw).content("{\"session_id\":" + s + ",\"score\":-1}"))
        .andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("FORBIDDEN"));
    mvc.perform(as(post("/feedback"), userLead, wsSw).content("{\"session_id\":" + s + ",\"score\":null}")).andExpect(status().isForbidden());
    assertThat(jdbc.sql("SELECT score FROM answer_feedback WHERE session_id = ?").param(s).query(Integer.class).single()).isEqualTo(1);
    mvc.perform(as(post("/feedback"), userBackend, wsSw)
            .content("{\"session_id\":" + s + ",\"score\":-1," + json("comment", "가".repeat(Limits.COMMENT + 1)) + "}"))
        .andExpect(status().isBadRequest());
    mvc.perform(as(post("/feedback"), userBackend, wsSw)
            .content("{\"session_id\":" + s + ",\"score\":-1," + json("comment", "가".repeat(Limits.COMMENT)) + "}"))
        .andExpect(status().isNoContent());
  }

  @Test
  void inviteCapsNameAndGradeAndReturnsTheRealName() throws Exception {
    long before = jdbc.sql("SELECT count(*) FROM users").query(Long.class).single();
    mvc.perform(as(post("/users"), userLead, wsSw).content("{\"employee_no\":\"T0801\"," + json("name", "가".repeat(Limits.NAME + 1)) + "}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    mvc.perform(as(post("/users"), userLead, wsSw)
            .content("{\"employee_no\":\"T0801\"," + json("name", "가") + "," + json("grade", "가".repeat(Limits.GRADE + 1)) + "}"))
        .andExpect(status().isBadRequest());
    mvc.perform(as(post("/users"), userLead, wsSw)
            .content("{\"employee_no\":\"T0801\"," + json("name", "가") + "," + json("position", "가".repeat(Limits.GRADE + 1)) + "}"))
        .andExpect(status().isBadRequest());
    mvc.perform(as(post("/users"), userLead, wsSw).content("{\"employee_no\":\"T0801\"," + json("name", "가") + "," + json("email", emailBackend) + "}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("EMAIL_TAKEN"));
    assertThat(jdbc.sql("SELECT count(*) FROM users").query(Long.class).single()).isEqualTo(before);
    // 이미 있는 계정을 부서에 더할 때 응답의 이름은 요청에 적은 이름이 아니라 그 계정의 이름이다
    mvc.perform(as(post("/users"), userBackend, wsHr).content("{\"employee_no\":\"" + empLead + "\"," + json("name", "아무 이름") + "}"))
        .andExpect(status().isCreated()).andExpect(jsonPath("$.created").value(false)).andExpect(jsonPath("$.name").value("배종현"));
  }

  private org.springframework.test.web.servlet.request.MockMultipartHttpServletRequestBuilder upload(String slug) {
    var b = multipart("/documents/uploads").file(new MockMultipartFile("file", "a.md", "application/octet-stream",
        "## 제1조 (목적) 가".getBytes(java.nio.charset.StandardCharsets.UTF_8)));
    b.param("slug", slug).cookie(loginCookie(userBackend)).header(com.oom.assist.common.context.TenantContextFilter.WORKSPACE_HEADER, wsHr);
    return b;
  }

  @Test
  void uploadCapsTitleAndVersion() throws Exception {
    mvc.perform(upload("limit-title").param("title", "가".repeat(Limits.DOC_TITLE + 1)).param("version", "v1"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    mvc.perform(upload("limit-version").param("title", "제목").param("version", "v".repeat(Limits.DOC_VERSION + 1)))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    // 빠진 칸도 같은 모양({code, message})으로 답한다
    mvc.perform(upload("limit-missing").param("version", "v1"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    assertThat(jdbc.sql("SELECT count(*) FROM policy_uploads WHERE slug LIKE 'limit-%'").query(Long.class).single()).isZero();
  }

  @Test
  void unreadableBodyIs400WithCodeEvenBeforeLogin() throws Exception {
    mvc.perform(post("/auth/login").contentType(MediaType.APPLICATION_JSON).content("\"그냥 글자\""))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    mvc.perform(as(post("/feedback"), userBackend, wsSw).content("{\"session_id\":1,\"score\":\"좋아요\"}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
  }
}
