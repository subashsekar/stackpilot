import { Controller, Get } from "@nestjs/common";

@Controller("health")
export class HealthController {
  @Get()
  check() {
    return { ok: true, service: "app_nestjs" };
  }
}

@Controller()
export class AppController {
  @Get("ping")
  ping() {
    return { pong: true };
  }
}
