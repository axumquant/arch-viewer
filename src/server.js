"use strict";

const express = require("express");
const http = require("http");
const { WebSocketServer } = require("ws");
const path = require("path");
const fs = require("fs");
const { scanProject, detectComponents } = require("./scanner");
const { FileWatcher } = require("./watcher");

function startServer({ projectRoot, port = 3777, autoOpen = true }) {
  const app = express();
  const server = http.createServer(app);
  const wss = new WebSocketServer({ server });

  // Serve static files — prefer web/ (enhanced dashboard), fallback to public/
  const webDir = path.join(__dirname, "..", "web");
  const publicDir = path.join(__dirname, "..", "public");
  const fs_sync = require("fs");
  if (fs_sync.existsSync(webDir)) {
    app.use(express.static(webDir));
  }
  app.use(express.static(publicDir));
  app.use(express.json({ limit: "5mb" }));

  // ---------- REST API ----------

  // GET /api/scan — full project scan
  app.get("/api/scan", (req, res) => {
    try {
      const { tree, files, stats } = scanProject(projectRoot);
      const components = detectComponents(projectRoot, tree);
      res.json({ root: path.basename(projectRoot), tree, files, stats, components });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  // GET /api/file?path=relative/path — read a file
  app.get("/api/file", (req, res) => {
    const relPath = req.query.path;
    if (!relPath) return res.status(400).json({ error: "path required" });

    // Security: prevent directory traversal
    const absPath = path.resolve(projectRoot, relPath);
    if (!absPath.startsWith(path.resolve(projectRoot))) {
      return res.status(403).json({ error: "Access denied" });
    }

    try {
      const stat = fs.statSync(absPath);
      if (stat.size > 2 * 1024 * 1024) {
        return res.status(413).json({ error: "File too large (>2MB)" });
      }

      // Check if binary
      const ext = path.extname(absPath).toLowerCase();
      const binaryExts = new Set([".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".zip", ".tar", ".gz", ".pdf"]);
      if (binaryExts.has(ext)) {
        return res.json({ path: relPath, binary: true, size: stat.size });
      }

      const content = fs.readFileSync(absPath, "utf8");
      res.json({
        path: relPath,
        content,
        size: stat.size,
        modified: stat.mtimeMs,
        binary: false,
      });
    } catch (err) {
      if (err.code === "ENOENT") return res.status(404).json({ error: "File not found" });
      res.status(500).json({ error: err.message });
    }
  });

  // PUT /api/file — write/update a file
  app.put("/api/file", (req, res) => {
    const { path: relPath, content } = req.body;
    if (!relPath || content === undefined) {
      return res.status(400).json({ error: "path and content required" });
    }

    const absPath = path.resolve(projectRoot, relPath);
    if (!absPath.startsWith(path.resolve(projectRoot))) {
      return res.status(403).json({ error: "Access denied" });
    }

    try {
      // Ensure parent directory exists
      fs.mkdirSync(path.dirname(absPath), { recursive: true });
      fs.writeFileSync(absPath, content, "utf8");
      const stat = fs.statSync(absPath);
      res.json({ ok: true, path: relPath, size: stat.size, modified: stat.mtimeMs });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  // GET /api/recent — recent file changes
  app.get("/api/recent", (req, res) => {
    res.json({ changes: watcher.getRecentChanges() });
  });

  // ---------- API Key Management ----------

  const KEYS_FILENAME = ".arch-viewer.keys.json";
  const PROVIDERS = {
    ollama: {
      display: "Ollama Cloud", env_var: "OLLAMA_API_KEY",
      models: [
        { id: "qwen3-coder:480b-cloud", name: "Qwen3 Coder 480B", recommended: true, description: "Best for code analysis and architecture review" },
        { id: "gpt-oss:120b-cloud", name: "GPT-OSS 120B", recommended: false, description: "Strong general-purpose model" },
        { id: "gpt-oss:20b-cloud", name: "GPT-OSS 20B", recommended: false, description: "Fast and lightweight" },
        { id: "deepseek-v3.1:671b-cloud", name: "DeepSeek V3.1 671B", recommended: false, description: "Largest, most capable reasoning model" },
      ],
      default_model: "qwen3-coder:480b-cloud",
    },
    openai: {
      display: "OpenAI", env_var: "OPENAI_API_KEY",
      models: [
        { id: "gpt-4.1-mini", name: "GPT-4.1 Mini", recommended: true, description: "Fast, cost-efficient — ideal for architecture scanning" },
        { id: "gpt-4.1", name: "GPT-4.1", recommended: false, description: "Latest flagship — best coding and instruction following" },
        { id: "gpt-4o", name: "GPT-4o", recommended: false, description: "Previous flagship, solid all-around" },
        { id: "gpt-4o-mini", name: "GPT-4o Mini", recommended: false, description: "Budget-friendly, good for simple scans" },
      ],
      default_model: "gpt-4.1-mini",
    },
    anthropic: {
      display: "Anthropic", env_var: "ANTHROPIC_API_KEY",
      models: [
        { id: "claude-sonnet-4-6", name: "Claude Sonnet 4.6", recommended: true, description: "Best balance — near-Opus quality at practical cost" },
        { id: "claude-haiku-4-5", name: "Claude Haiku 4.5", recommended: false, description: "Fastest and cheapest — great for quick scans" },
        { id: "claude-opus-4-6", name: "Claude Opus 4.6", recommended: false, description: "Most capable — deep architecture analysis" },
      ],
      default_model: "claude-sonnet-4-6",
    },
    groq: {
      display: "Groq", env_var: "GROQ_API_KEY",
      models: [
        { id: "llama-3.3-70b-versatile", name: "Llama 3.3 70B", recommended: true, description: "Best general-purpose — fast inference on Groq" },
        { id: "meta-llama/llama-4-scout-17b-16e-instruct", name: "Llama 4 Scout", recommended: false, description: "Latest Llama 4 with 10M context" },
        { id: "llama-3.1-8b-instant", name: "Llama 3.1 8B Instant", recommended: false, description: "Ultra-fast, lowest latency" },
        { id: "gemma2-9b-it", name: "Gemma 2 9B", recommended: false, description: "Google's efficient open model" },
      ],
      default_model: "llama-3.3-70b-versatile",
    },
  };

  function loadKeys() {
    const keysPath = path.join(projectRoot, KEYS_FILENAME);
    try {
      if (fs.existsSync(keysPath)) {
        return JSON.parse(fs.readFileSync(keysPath, "utf8"));
      }
    } catch { /* skip */ }
    return {};
  }

  function saveKeys(newKeys, selectedModels) {
    const keysPath = path.join(projectRoot, KEYS_FILENAME);
    const existing = loadKeys();
    for (const [k, v] of Object.entries(newKeys)) {
      if (typeof v === "string" && v.trim() && PROVIDERS[k]) {
        existing[k] = v.trim();
      }
    }
    if (selectedModels && typeof selectedModels === "object") {
      if (!existing.selected_models) existing.selected_models = {};
      Object.assign(existing.selected_models, selectedModels);
    }
    fs.writeFileSync(keysPath, JSON.stringify(existing, null, 2) + "\n", "utf8");
    return existing;
  }

  // GET /api/keys — providers, keys status, model catalog
  app.get("/api/keys", (req, res) => {
    const data = loadKeys();
    const status = {};
    for (const [id, info] of Object.entries(PROVIDERS)) {
      const selectedModels = data.selected_models || {};
      status[id] = {
        configured: !!(data[id] || process.env[info.env_var]),
        display: info.display,
        env_var: info.env_var,
        models: info.models,
        default_model: info.default_model,
        selected_model: selectedModels[id] || info.default_model,
      };
    }
    const providerOrder = ["ollama", "openai", "anthropic", "groq"];
    const active = providerOrder.find((p) => status[p].configured) || "none";
    res.json({
      providers: status,
      active_provider: active,
      ai_enabled: false,
    });
  });

  // POST /api/keys — save keys + model selections
  app.post("/api/keys", (req, res) => {
    const { keys, selected_models } = req.body;
    if (!keys && !selected_models) {
      return res.status(400).json({ error: "keys or selected_models required" });
    }
    saveKeys(keys || {}, selected_models || null);
    const allData = loadKeys();
    const providerOrder = ["ollama", "openai", "anthropic", "groq"];
    const active = providerOrder.find((p) => !!allData[p]) || "none";
    const selectedModel = (allData.selected_models || {})[active] || PROVIDERS[active]?.default_model || "";
    res.json({ ok: true, active_provider: active, selected_model: selectedModel, ai_enabled: false });
  });

  // GET /api/search?q=term — simple text search across files
  app.get("/api/search", (req, res) => {
    const query = req.query.q;
    if (!query) return res.status(400).json({ error: "q required" });

    const { files } = scanProject(projectRoot, { maxFiles: 1000 });
    const results = [];
    const MAX_RESULTS = 50;

    for (const file of files) {
      if (results.length >= MAX_RESULTS) break;
      if (file.size > 500000) continue; // skip large files

      const absPath = path.join(projectRoot, file.path);
      try {
        const content = fs.readFileSync(absPath, "utf8");
        const lines = content.split("\n");
        for (let i = 0; i < lines.length; i++) {
          if (lines[i].toLowerCase().includes(query.toLowerCase())) {
            results.push({
              file: file.path,
              line: i + 1,
              text: lines[i].trim().substring(0, 200),
            });
            if (results.length >= MAX_RESULTS) break;
          }
        }
      } catch { /* skip */ }
    }

    res.json({ query, results, total: results.length });
  });

  // ---------- v2: Scoring, Dependency Graph, Anti-Patterns ----------

  // GET /api/score — architecture health score (stub — full scoring requires Python AST)
  app.get("/api/score", (req, res) => {
    try {
      const { files, stats } = scanProject(projectRoot);
      const components = detectComponents(projectRoot, scanProject(projectRoot).tree);

      // Basic scoring heuristics for the Node.js server
      let modularity = 25, quality = 25, maintainability = 25, structure = 25;
      const antiPatterns = [];

      // Modularity
      if (components.length === 0) { modularity -= 10; }
      else if (components.length === 1) { modularity -= 5; }

      // Quality — check for large files
      const largeFiles = files.filter(f => f.size > 50000);
      if (largeFiles.length > 5) { quality -= 5; }

      // Maintainability
      if (!fs.existsSync(path.join(projectRoot, "README.md"))) { maintainability -= 3; }
      const hasTests = ["tests", "test", "__tests__", "spec"].some(d =>
        fs.existsSync(path.join(projectRoot, d))
      );
      if (!hasTests) { maintainability -= 5; }

      // Structure
      if (!fs.existsSync(path.join(projectRoot, ".gitignore"))) { structure -= 2; }

      // Large files anti-pattern
      for (const f of largeFiles) {
        if (f.size > 100000) {
          antiPatterns.push({
            name: "Large File",
            severity: "warning",
            description: `${f.path} is ${(f.size / 1024).toFixed(0)}KB`,
            file: f.path,
            suggestion: "Consider splitting into smaller modules",
          });
        }
      }

      const total = Math.max(0, modularity + quality + maintainability + structure);
      const grade = total >= 90 ? "A" : total >= 80 ? "B" : total >= 70 ? "C" : total >= 60 ? "D" : "F";

      res.json({
        total, grade,
        categories: [
          { name: "Modularity", score: Math.max(0, modularity), max_score: 25, details: [], penalties: [] },
          { name: "Code Quality", score: Math.max(0, quality), max_score: 25, details: [], penalties: [] },
          { name: "Maintainability", score: Math.max(0, maintainability), max_score: 25, details: [], penalties: [] },
          { name: "Structure", score: Math.max(0, structure), max_score: 25, details: [], penalties: [] },
        ],
        anti_patterns: antiPatterns,
        recommendations: antiPatterns.length > 0 ? ["Address large file warnings"] : ["Architecture looks healthy"],
        file_count: stats.totalFiles,
        analyzed_count: files.length,
        note: "Full AST-based scoring requires the Python server (python -m arch_viewer)",
      });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  // GET /api/dep-graph — basic dependency graph
  app.get("/api/dep-graph", (req, res) => {
    try {
      const { files } = scanProject(projectRoot);
      const graphType = req.query.type || "components";
      const components = detectComponents(projectRoot, scanProject(projectRoot).tree);

      const nodes = components.map(c => ({
        id: `comp:${c.name}`,
        label: c.name,
        kind: "component",
        group: c.type,
        size: 3,
        metadata: { type: c.type, tech: c.tech, path: c.path },
      }));

      // Infer edges between components
      const edges = [];
      const hasFE = components.find(c => c.type === "frontend");
      const hasBE = components.find(c => c.type === "backend");
      const hasExt = components.find(c => c.type === "extension");
      if (hasFE && hasBE) {
        edges.push({ source: `comp:${hasFE.name}`, target: `comp:${hasBE.name}`, kind: "HTTP/REST", label: "API calls" });
      }
      if (hasExt && hasBE) {
        edges.push({ source: `comp:${hasExt.name}`, target: `comp:${hasBE.name}`, kind: "WebSocket", label: "Real-time" });
      }

      res.json({
        nodes, edges, clusters: {},
        stats: { node_count: nodes.length, edge_count: edges.length, cluster_count: 0 },
        hotspots: [],
        note: "Full import/call graphs require the Python server (python -m arch_viewer)",
      });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  // GET /api/anti-patterns — basic anti-pattern detection
  app.get("/api/anti-patterns", (req, res) => {
    try {
      const { files } = scanProject(projectRoot);
      const patterns = [];

      for (const f of files) {
        if (f.size > 100000) {
          patterns.push({
            name: "Large File",
            severity: "warning",
            description: `${f.path} is ${(f.size / 1024).toFixed(0)}KB — may be a god file`,
            file: f.path,
            suggestion: "Consider splitting into smaller, focused modules",
          });
        }
      }

      res.json({
        anti_patterns: patterns,
        total: patterns.length,
        by_severity: {
          critical: patterns.filter(p => p.severity === "critical").length,
          warning: patterns.filter(p => p.severity === "warning").length,
          info: patterns.filter(p => p.severity === "info").length,
        },
        note: "Full anti-pattern detection (circular imports, AST analysis) requires the Python server",
      });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  // ---------- WebSocket ----------

  // Transform scanner output to match the Architecture model shape
  // that web/index.html (enhanced dashboard) expects
  function buildArchPayload() {
    const { tree, files, stats } = scanProject(projectRoot);
    const rawComponents = detectComponents(projectRoot, tree);

    // Convert flat file list into nested tree dict
    const fileTree = {};
    for (const f of files) {
      const parts = f.path.split("/");
      let node = fileTree;
      for (let i = 0; i < parts.length; i++) {
        const part = parts[i];
        if (i === parts.length - 1) {
          node[part] = { _file: f };
        } else {
          if (!node[part]) node[part] = {};
          node = node[part];
        }
      }
    }

    // Map components to Architecture model shape
    const components = rawComponents.map((c) => ({
      name: c.name,
      type: c.type,
      path: c.path,
      tech_stack: c.tech ? [c.tech] : [],
      description: "",
      files: files.filter((f) => f.path.startsWith(c.path === "." ? "" : c.path + "/")).map((f) => f.path),
      api_routes: [],
      entry_points: [],
      env_vars: [],
    }));

    // Infer data flows
    const data_flows = [];
    const names = components.map((c) => c.name);
    const hasFrontend = components.find((c) => c.type === "frontend");
    const hasBackend = components.find((c) => c.type === "backend");
    const hasExt = components.find((c) => c.type === "extension");
    if (hasFrontend && hasBackend) {
      data_flows.push({ source: hasFrontend.name, target: hasBackend.name, protocol: "HTTP/REST", description: "API calls", direction: "bidirectional" });
    }
    if (hasExt && hasBackend) {
      data_flows.push({ source: hasExt.name, target: hasBackend.name, protocol: "WebSocket", description: "Real-time messaging", direction: "bidirectional" });
    }

    return {
      type: "init",
      project_name: path.basename(projectRoot),
      root: path.basename(projectRoot),
      components,
      data_flows,
      dependencies: [],
      file_tree: fileTree,
      stats: {
        total_files: stats.totalFiles,
        total_size: stats.totalSize,
        by_category: stats.byCategory,
        by_language: stats.byLanguage,
      },
      ai_summary: "",
      last_analyzed: Date.now() / 1000,
      analysis_version: 0,
      // Legacy fields for public/index.html compat
      tree,
      files,
    };
  }

  wss.on("connection", (ws) => {
    // Send initial scan on connect
    try {
      ws.send(JSON.stringify(buildArchPayload()));
    } catch (err) {
      ws.send(JSON.stringify({ type: "error", message: err.message }));
    }

    // Handle messages from client
    ws.on("message", (raw) => {
      try {
        const msg = JSON.parse(raw);

        if (msg.type === "read_file") {
          const absPath = path.resolve(projectRoot, msg.path);
          if (!absPath.startsWith(path.resolve(projectRoot))) {
            ws.send(JSON.stringify({ type: "error", message: "Access denied" }));
            return;
          }
          const content = fs.readFileSync(absPath, "utf8");
          ws.send(JSON.stringify({
            type: "file_content",
            path: msg.path,
            content,
            requestId: msg.requestId,
          }));
        }

        if (msg.type === "save_file") {
          const absPath = path.resolve(projectRoot, msg.path);
          if (!absPath.startsWith(path.resolve(projectRoot))) {
            ws.send(JSON.stringify({ type: "error", message: "Access denied" }));
            return;
          }
          fs.mkdirSync(path.dirname(absPath), { recursive: true });
          fs.writeFileSync(absPath, msg.content, "utf8");
          ws.send(JSON.stringify({
            type: "file_saved",
            path: msg.path,
            requestId: msg.requestId,
          }));
        }

        if (msg.type === "rescan" || msg.type === "refresh_ai") {
          ws.send(JSON.stringify(buildArchPayload()));
        }
      } catch (err) {
        ws.send(JSON.stringify({ type: "error", message: err.message }));
      }
    });
  });

  // ---------- File Watcher ----------

  const watcher = new FileWatcher(projectRoot, wss);
  watcher.start();

  // ---------- Start ----------

  server.listen(port, () => {
    const url = `http://localhost:${port}`;
    console.log(`\n  🏗️  Arch Viewer running at ${url}`);
    console.log(`  📂 Watching: ${projectRoot}`);
    console.log(`  🔌 WebSocket: ws://localhost:${port}`);
    console.log(`  📡 API: ${url}/api/scan\n`);

    if (autoOpen) {
      import("open").then((mod) => mod.default(url)).catch(() => {
        // open module not available, skip
        console.log("  (auto-open not available — visit the URL manually)\n");
      });
    }
  });

  // Graceful shutdown
  process.on("SIGINT", () => {
    console.log("\n  Shutting down...");
    watcher.stop();
    server.close();
    process.exit(0);
  });

  return { app, server, wss, watcher };
}

module.exports = { startServer };
