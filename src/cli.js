#!/usr/bin/env node
"use strict";

const path = require("path");
const minimist = require("minimist");

const argv = minimist(process.argv.slice(2), {
  default: { port: 3777, open: true },
  alias: { p: "port", o: "open", h: "help", d: "dir" },
  boolean: ["open", "help"],
});

if (argv.help) {
  console.log(`
  arch-viewer — Live interactive architecture viewer

  Usage:
    arch-viewer [options] [directory]

  Options:
    -p, --port <n>   Port to listen on (default: 3777)
    -d, --dir <path> Project root to scan (default: cwd)
    -o, --open       Auto-open browser (default: true)
    --no-open        Don't auto-open browser
    -h, --help       Show this help

  Examples:
    arch-viewer                    # Scan current directory
    arch-viewer ./my-project       # Scan specific project
    arch-viewer -p 4000 --no-open  # Custom port, no browser
`);
  process.exit(0);
}

const projectRoot = path.resolve(argv.dir || argv._[0] || process.cwd());
const port = argv.port;

const { startServer } = require("./server");

startServer({ projectRoot, port, autoOpen: argv.open });
