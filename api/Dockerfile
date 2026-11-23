# Spring API 이미지. 빌드는 이미지 안에서(박스에서 빌드하지 않는다 — Actions 또는 개발 PC 에서 build 후 ghcr 로).
FROM eclipse-temurin:21-jdk AS build
WORKDIR /src
COPY gradlew build.gradle.kts settings.gradle.kts ./
COPY gradle gradle
RUN chmod +x gradlew && ./gradlew --no-daemon --version >/dev/null
COPY src src
RUN ./gradlew --no-daemon bootJar -x test

FROM eclipse-temurin:21-jre
# healthcheck 용 curl
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /src/build/libs/*.jar app.jar
# 4 GB 박스 기준. 2 GB 로 내리면 -Xmx384m
# 힙 512MB — 컨테이너 한도 1GB 안에 힙 밖(메타스페이스 · 코드 캐시 · 스레드)이 들어가게. 한도 1GB 에서는 JVM 이 어차피 SerialGC 를 고른다
ENV JAVA_TOOL_OPTIONS="-Xmx512m -XX:+UseSerialGC -Djava.security.egd=file:/dev/./urandom"
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
