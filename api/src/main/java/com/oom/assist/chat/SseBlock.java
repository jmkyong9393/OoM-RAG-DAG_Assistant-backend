package com.oom.assist.chat;

import java.util.ArrayList;
import java.util.List;

/** SSE 블록 하나 (event 이름 + data 본문). 줄 단위 스트림에서 빈 줄을 만나면 완성된다. */
public record SseBlock(String event, String data) {

  /** 줄을 누적하다 빈 줄에서 블록을 만든다. 파서 상태는 호출자가 보관한다. */
  public static final class Parser {
    private String event = "message";
    private final List<String> data = new ArrayList<>();

    /** 줄 하나를 먹고, 블록이 완성되면 돌려준다(아니면 null). */
    public SseBlock feed(String line) {
      if (line.isEmpty()) {
        if (data.isEmpty()) { event = "message"; return null; }
        SseBlock b = new SseBlock(event, String.join("\n", data));
        event = "message";
        data.clear();
        return b;
      }
      if (line.startsWith("event:")) event = line.substring(6).trim();
      else if (line.startsWith("data:")) data.add(line.substring(5).replaceFirst("^ ", ""));
      // id:, retry:, 주석(:) 은 무시
      return null;
    }

    /** 스트림이 빈 줄 없이 끝났을 때 남은 블록. */
    public SseBlock flush() {
      return feed("");
    }
  }
}
