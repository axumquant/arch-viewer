"use strict";

const chokidar = require("chokidar");
const path = require("path");
const fs = require("fs");
const { getCategory, getLanguage } = require("./scanner");

/**
 * FileWatcher — watches a project root and emits events over WebSocket
 * to all connected clients for real-time architecture updates.
 */
class FileWatcher {
  constructor(rootDir, wss) {
    this.rootDir = rootDir;
    this.wss = wss;
    this.watcher = null;
    this.recentChanges = []; // ring buffer of last 50 changes
    this.MAX_RECENT = 50;
  }

  start() {
    this.watcher = chokidar.watch(this.rootDir, {
      ignored: [
        /(^|[\/\\])\./,          // dotfiles (except .claude, .github)
        "**/node_modules/**",
        "**/__pycache__/**",
        "**/.venv/**",
        "**/venv/**",
        "**/.next/**",
        "**/dist/**",
        "**/build/**",
        "**/.git/**",
        "**/*.pyc",
      ],
      persistent: true,
      ignoreInitial: true,
      awaitWriteFinish: {
        stabilityThreshold: 300,
        pollInterval: 100,
      },
    });

    const handleEvent = (eventType, filePath) => {
      const relPath = path.relative(this.rootDir, filePath).replace(/\\/g, "/");
      if (relPath.startsWith(".git/")) return; // extra safety

      let stat = null;
      try {
        if (eventType !== "unlink" && eventType !== "unlinkDir") {
          stat = fs.statSync(filePath);
        }
      } catch { /* deleted between event and stat */ }

      const event = {
        type: "file_change",
        event: eventType,
        path: relPath,
        name: path.basename(filePath),
        category: getCategory(relPath),
        language: getLanguage(relPath),
        size: stat ? stat.size : 0,
        modified: stat ? stat.mtimeMs : Date.now(),
        timestamp: Date.now(),
      };

      // Store in ring buffer
      this.recentChanges.push(event);
      if (this.recentChanges.length > this.MAX_RECENT) {
        this.recentChanges.shift();
      }

      // Broadcast to all connected WebSocket clients
      this.broadcast(event);
    };

    this.watcher
      .on("add", (p) => handleEvent("add", p))
      .on("change", (p) => handleEvent("change", p))
      .on("unlink", (p) => handleEvent("unlink", p))
      .on("addDir", (p) => handleEvent("addDir", p))
      .on("unlinkDir", (p) => handleEvent("unlinkDir", p));

    // Also emit .claude-specific events (ignored patterns don't apply to .claude)
    const claudeWatcher = chokidar.watch(path.join(this.rootDir, ".claude"), {
      ignored: ["**/.git/**"],
      persistent: true,
      ignoreInitial: true,
      awaitWriteFinish: { stabilityThreshold: 300, pollInterval: 100 },
    });

    claudeWatcher
      .on("add", (p) => handleEvent("add", p))
      .on("change", (p) => handleEvent("change", p))
      .on("unlink", (p) => handleEvent("unlink", p));

    this._claudeWatcher = claudeWatcher;
  }

  broadcast(data) {
    const msg = JSON.stringify(data);
    for (const client of this.wss.clients) {
      if (client.readyState === 1) { // WebSocket.OPEN
        client.send(msg);
      }
    }
  }

  getRecentChanges() {
    return [...this.recentChanges];
  }

  stop() {
    if (this.watcher) this.watcher.close();
    if (this._claudeWatcher) this._claudeWatcher.close();
  }
}

module.exports = { FileWatcher };
