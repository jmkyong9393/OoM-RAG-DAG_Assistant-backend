package com.oom.assist;

import org.springframework.boot.SpringApplication;

public class TestOomAssistApiApplication {

	public static void main(String[] args) {
		SpringApplication.from(OomAssistApiApplication::main).with(TestcontainersConfiguration.class).run(args);
	}

}
