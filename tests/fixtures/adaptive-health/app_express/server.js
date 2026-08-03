const express = require("express");

const port = Number(process.env.PORT || 8000);
const app = express();
const router = express.Router();

router.get("/health", (_req, res) => {
  res.json({ ok: true, service: "app_express" });
});

app.use("/internal", router);
app.get("/ping", (_req, res) => res.send("pong"));
app.get("/", (_req, res) => res.send("express ok"));

app.listen(port, "0.0.0.0", () => {
  console.log(`express adaptive-health demo on ${port}`);
});
