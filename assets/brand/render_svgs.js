"use strict";
const fs = require("node:fs");
const sharp = require("sharp");

async function main() {
  const jobs = JSON.parse(fs.readFileSync(0, "utf8"));
  for (const job of jobs) {
    await sharp(job.source, { density: 384 })
      .resize(job.width, job.height, { fit: "fill" })
      .png()
      .toFile(job.output);
  }
}
main().catch((err) => { console.error(err.stack || err); process.exitCode = 1; });
