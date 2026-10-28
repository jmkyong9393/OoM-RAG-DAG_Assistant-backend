plugins {
	// Java 21 툴체인을 로컬에 없으면 자동 내려받는다 (호스트 JDK 17로도 빌드 가능)
	id("org.gradle.toolchains.foojay-resolver-convention") version "1.0.0"
}

rootProject.name = "api"
