"use strict";

const { spawnSync } = require("node:child_process");
const path = require("node:path");
const { version } = require("./package.json");

const assets = path.join(__dirname, "dist");
const child = spawnSync("sh", [path.join(assets, "install.sh")], {
  stdio: "inherit",
  env: {
    ...process.env,
    MUNINN_ARCHIVE_DIR: assets,
    MUNINN_INSTALL_DIR: path.join(__dirname, "runtime"),
    MUNINN_VERSION: version
  }
});
if (child.error) {
  console.error(`muninn: could not start the parser installer: ${child.error.message}`);
  process.exitCode = 1;
} else {
  process.exitCode = child.status ?? 1;
}
