import { Controller, Get } from "@nestjs/common";

@Controller()
export class AppController {
  @Get()
  root() {
    return { ok: true };
  }

  @Get("health")
  health() {
    return { ok: true };
  }
}
