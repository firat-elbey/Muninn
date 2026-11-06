#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const { existsSync } = require("node:fs");
const path = require("node:path");
const { version } = require("../package.json");

const root = path.join(__dirname, "..");
const runtime = path.join(root, "runtime", "muninn");
const args = process.argv.slice(2);
let command = runtime;
if (!existsSync(runtime)) {
  console.error(
    "muninn: using the core archive. To enable parsers for a global install, " +
    "run npm explore -g muninn-kb -- node provision.cjs. For a local install, omit -g."
  );
  command = "python3";
  args.unshift("-I", "-S", path.join(root, "dist", `muninn-${version}.pyz`));
}

const child = spawnSync(command, args, { stdio: "inherit" });
if (child.error) {
  console.error(`muninn: could not start ${command}: ${child.error.message}`);
  process.exitCode = 1;
} else if (child.signal) {
  process.kill(process.pid, child.signal);
} else {
  process.exitCode = child.status;
}
