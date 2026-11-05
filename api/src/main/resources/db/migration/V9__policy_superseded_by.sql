-- 개정 사슬: 이 문서를 대체한 문서. 사례의 근거 조문(attributes.policy_refs)이 폐기된 문서를 가리킬 때 지금 유효한 문서를 찾는 데 쓴다.
ALTER TABLE policy_documents ADD COLUMN IF NOT EXISTS superseded_by bigint REFERENCES policy_documents(id);
CREATE INDEX IF NOT EXISTS idx_policy_docs_superseded_by ON policy_documents (superseded_by) WHERE superseded_by IS NOT NULL;
