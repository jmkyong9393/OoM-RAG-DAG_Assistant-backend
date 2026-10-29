package com.oom.assist.unit;

import static org.assertj.core.api.Assertions.assertThat;

import com.oom.assist.auth.PasswordPolicy;
import com.oom.assist.common.util.Mask;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

/** 비밀번호 작성규칙(보호조치 기준 §4 ⑧)과 표시 제한(§10) 단위 테스트. */
class PasswordPolicyTest {

  @ParameterizedTest(name = "[{index}] {0} → valid={1}")
  @CsvSource({
      "'Oom!demo-2026', true",      // 4종 13자
      "'abcdefghij1', false",       // 2종 11자지만 abc… 연속
      "'qwerty!2026', true",        // 3종 11자 (qwerty 는 ±1 연속이 아님)
      "'Passw0rd', true",           // 3종 8자
      "'password12', false",        // 2종 10자이나 '12'… 아니, 연속은 2자뿐 → 통과해야 하나? 아래 참고
      "'passwordXY', true",         // 2종 10자
      "'short1!', false",           // 7자
      "'aaaBBB1234', false",        // 같은 문자 3연속
      "'Hello 2026!', false",       // 공백
      "'OOM0002pass!', false",      // 사번 포함
  })
  void rules(String pw, boolean valid) {
    // 'password12' 는 'sw' 'wo' 등 연속 없음, 숫자 '12' 는 2자 → 규칙상 통과. CsvSource 의 기대값을 그에 맞춘다.
    if (pw.equals("password12")) valid = true;
    assertThat(PasswordPolicy.isValid(pw, "OOM0002")).as(pw).isEqualTo(valid);
  }

  @Test
  void violationMessagesAreSpecific() {
    assertThat(PasswordPolicy.violations("oom0002abc", "OOM0002"))
        .anyMatch(m -> m.contains("사번")).anyMatch(m -> m.contains("연속"));
    assertThat(PasswordPolicy.violations("Oom!demo-2026", "OOM0002")).isEmpty();
    assertThat(PasswordPolicy.violations("a".repeat(129) + "B1", "X")).anyMatch(m -> m.contains("128"));
  }

  @Test
  void runsDetectBothDirectionsAndDigits() {
    assertThat(PasswordPolicy.hasRun("x321y")).isTrue();
    assertThat(PasswordPolicy.hasRun("xCBAy")).isTrue();
    assertThat(PasswordPolicy.hasRun("a1b2c3")).isFalse();
    assertThat(PasswordPolicy.hasRun("!!!")).isTrue();
  }

  @Test
  void masking() {
    assertThat(Mask.email("jang@oom.local")).isEqualTo("j***@oom.local");
    assertThat(Mask.email("x")).isEqualTo("***");
    assertThat(Mask.email(null)).isNull();
    assertThat(Mask.employeeNo("OOM0002")).isEqualTo("OOM00**");
  }
}
