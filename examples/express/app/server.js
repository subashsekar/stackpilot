const express = require("express");

const port = Number(process.env.PORT || 8000);
const app = express();

app.get("/", (_req, res) => {
  res.type("text").send("ok");
});

app.listen(port, "0.0.0.0", () => {
  console.log(`express example listening on ${port}`);
});
