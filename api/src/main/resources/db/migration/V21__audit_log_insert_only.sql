-- 접속기록은 앱이 덧붙이기만 한다(위 · 변조 방지): V2 가 INSERT 와 함께 준 UPDATE 를 거둔다. 앱 코드는 audit_log 를 고치지 않는다
REVOKE UPDATE ON audit_log FROM oom_api;
