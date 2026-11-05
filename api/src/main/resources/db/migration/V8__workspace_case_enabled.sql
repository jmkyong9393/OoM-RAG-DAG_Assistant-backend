-- 부서 유형 = 규정 사용(policy_enabled) × 사례 사용(case_enabled).
--   둘 다: 규정+사례형 / 규정만: 규정 참조형 / 사례만: 사례 단일형. 둘 다 끌 수는 없다.
ALTER TABLE workspaces ADD COLUMN case_enabled boolean NOT NULL DEFAULT true;
ALTER TABLE workspaces ADD CONSTRAINT workspaces_some_source CHECK (policy_enabled OR case_enabled);
