package com.oom.assist.unit;

import static org.assertj.core.api.Assertions.assertThat;

import com.oom.assist.chat.SseBlock;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

/** 줄 단위 SSE 파서. 블록 경계·여러 data 줄·주석·마지막 블록 flush. */
class SseBlockParserTest {

  private static List<SseBlock> parse(String... lines) {
    SseBlock.Parser p = new SseBlock.Parser();
    List<SseBlock> out = new ArrayList<>();
    for (String l : lines) { SseBlock b = p.feed(l); if (b != null) out.add(b); }
    SseBlock last = p.flush();
    if (last != null) out.add(last);
    return out;
  }

  @Test
  void splitsBlocksOnBlankLines() {
    List<SseBlock> blocks = parse("event: stage", "data: {\"node\":\"supervisor\"}", "", "event: token", "data: {\"text\":\"a\"}", "");
    assertThat(blocks).containsExactly(new SseBlock("stage", "{\"node\":\"supervisor\"}"), new SseBlock("token", "{\"text\":\"a\"}"));
  }

  @Test
  void joinsMultipleDataLinesAndDefaultsEventName() {
    List<SseBlock> blocks = parse("data: line1", "data:line2", "");
    assertThat(blocks).containsExactly(new SseBlock("message", "line1\nline2"));
  }

  @Test
  void ignoresCommentsIdsAndEmptyBlocks() {
    List<SseBlock> blocks = parse(": keepalive", "", "id: 7", "retry: 1000", "", "event: done", "data: {}", "");
    assertThat(blocks).containsExactly(new SseBlock("done", "{}"));
  }

  @Test
  void flushReturnsTrailingBlockWithoutBlankLine() {
    List<SseBlock> blocks = parse("event: done", "data: {\"ok\":true}");
    assertThat(blocks).containsExactly(new SseBlock("done", "{\"ok\":true}"));
  }
}
