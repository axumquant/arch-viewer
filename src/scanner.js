"use strict";

const fs = require("fs");
const path = require("path");

// Directories to always skip
const IGNORE_DIRS = new Set([
  "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
  ".next", ".nuxt", "dist", "build", ".turbo", ".cache",
  ".mypy_cache", ".pytest_cache", ".ruff_cache", "htmlcov",
  "coverage", ".tox", "eggs", "*.egg-info", ".terraform",
]);

// File extensions grouped by category
const CATEGORIES = {
  backend:    [".py", ".pyi"],
  frontend:   [".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte"],
  config:     [".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env"],
  style:      [".css", ".scss", ".sass", ".less"],
  template:   [".html", ".ejs", ".hbs", ".jinja2", ".j2"],
  data:       [".sql", ".graphql", ".gql", ".prisma"],
  docs:       [".md", ".rst", ".txt"],
  docker:     ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"],
  ci:         [".github", ".gitlab-ci.yml", "Jenkinsfile"],
  shell:      [".sh", ".bash", ".zsh", ".ps1"],
  extension:  [".crx"],
};

function getCategory(filePath) {
  const base = path.basename(filePath);
  const ext = path.extname(filePath).toLowerCase();

  if (base === "Dockerfile" || base.startsWith("docker-compose")) return "docker";
  if (base === "Jenkinsfile") return "ci";
  if (filePath.includes(".github")) return "ci";
  if (base === "manifest.json" && filePath.includes("extension")) return "extension";

  for (const [cat, exts] of Object.entries(CATEGORIES)) {
    if (exts.includes(ext)) return cat;
  }
  return "other";
}

function getLanguage(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  const map = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".md": "markdown",
    ".html": "html", ".css": "css", ".scss": "scss",
    ".sql": "sql", ".sh": "shell", ".bash": "shell",
    ".graphql": "graphql", ".prisma": "prisma",
    ".vue": "vue", ".svelte": "svelte",
    ".rs": "rust", ".go": "go", ".java": "java",
    ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".cs": "csharp", ".cpp": "cpp",
    ".c": "c", ".h": "c", ".hpp": "cpp",
  };
  return map[ext] || "text";
}

/**
 * Scan a project directory and build a file tree with metadata.
 * Returns { tree, files, stats }
 */
function scanProject(rootDir, opts = {}) {
  const maxDepth = opts.maxDepth || 12;
  const maxFiles = opts.maxFiles || 5000;
  const files = [];
  let fileCount = 0;

  function shouldIgnore(name) {
    if (name.startsWith(".") && name !== ".claude" && name !== ".github") return true;
    return IGNORE_DIRS.has(name);
  }

  function walkDir(dir, depth, relPath) {
    if (depth > maxDepth || fileCount >= maxFiles) return null;

    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return null;
    }

    const node = {
      name: path.basename(dir),
      path: relPath || ".",
      type: "directory",
      children: [],
    };

    // Sort: dirs first, then files, alphabetical within each
    entries.sort((a, b) => {
      if (a.isDirectory() && !b.isDirectory()) return -1;
      if (!a.isDirectory() && b.isDirectory()) return 1;
      return a.name.localeCompare(b.name);
    });

    for (const entry of entries) {
      const entryPath = path.join(dir, entry.name);
      const entryRelPath = relPath ? `${relPath}/${entry.name}` : entry.name;

      if (entry.isDirectory()) {
        if (shouldIgnore(entry.name)) continue;
        const child = walkDir(entryPath, depth + 1, entryRelPath);
        if (child && child.children.length > 0) {
          node.children.push(child);
        }
      } else if (entry.isFile()) {
        if (fileCount >= maxFiles) break;
        fileCount++;

        let stat;
        try {
          stat = fs.statSync(entryPath);
        } catch {
          continue;
        }

        const fileNode = {
          name: entry.name,
          path: entryRelPath,
          type: "file",
          size: stat.size,
          modified: stat.mtimeMs,
          category: getCategory(entryRelPath),
          language: getLanguage(entryRelPath),
        };

        node.children.push(fileNode);
        files.push(fileNode);
      }
    }

    return node;
  }

  const tree = walkDir(rootDir, 0, "");

  // Build stats
  const stats = {
    totalFiles: files.length,
    byCategory: {},
    byLanguage: {},
    totalSize: 0,
  };

  for (const f of files) {
    stats.byCategory[f.category] = (stats.byCategory[f.category] || 0) + 1;
    stats.byLanguage[f.language] = (stats.byLanguage[f.language] || 0) + 1;
    stats.totalSize += f.size;
  }

  return { tree, files, stats };
}

/**
 * Detect project components (backend, frontend, extension, etc.)
 */
function detectComponents(rootDir, tree) {
  const components = [];

  function findDir(node, name) {
    if (!node || !node.children) return null;
    for (const child of node.children) {
      if (child.type === "directory" && child.name === name) return child;
    }
    return null;
  }

  function hasFile(dirPath, fileName) {
    try { return fs.existsSync(path.join(rootDir, dirPath, fileName)); }
    catch { return false; }
  }

  // Detect backend — check multiple common layouts
  const backendDetected = (() => {
    // Monorepo: backend/ directory
    if (hasFile("backend", "pyproject.toml") || hasFile("backend", "requirements.txt")) {
      return { path: "backend" };
    }
    // Python src/ layout (sales-coach pattern)
    if (hasFile("src", "main.py") || hasFile("src", "__init__.py")) {
      return { path: "src" };
    }
    // Root-level Python project
    if (hasFile("", "pyproject.toml") || hasFile("", "requirements.txt")) {
      // Check if there's an app/ or src/ dir with Python files
      if (hasFile("app", "main.py") || hasFile("app", "__init__.py")) return { path: "app" };
      if (hasFile("src", "main.py")) return { path: "src" };
      return { path: "." };
    }
    return null;
  })();

  if (backendDetected) {
    // Detect specific framework
    let tech = "Python";
    try {
      const pyFiles = ["pyproject.toml", "requirements.txt"].map((f) =>
        [path.join(rootDir, f), path.join(rootDir, backendDetected.path, f)]
      ).flat();
      for (const fp of pyFiles) {
        if (fs.existsSync(fp)) {
          const txt = fs.readFileSync(fp, "utf8").toLowerCase();
          if (txt.includes("fastapi")) { tech = "Python / FastAPI"; break; }
          if (txt.includes("django")) { tech = "Python / Django"; break; }
          if (txt.includes("flask")) { tech = "Python / Flask"; break; }
        }
      }
    } catch { /* fallback */ }

    components.push({
      name: backendDetected.path === "src" ? "Source" : "Backend",
      type: "backend",
      path: backendDetected.path,
      tech,
      icon: "🐍",
    });
  }

  // Detect frontend / portal
  for (const dir of ["portal", "frontend", "web", "app", "client"]) {
    if (hasFile(dir, "package.json")) {
      const pkgPath = path.join(rootDir, dir, "package.json");
      try {
        const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
        const deps = { ...pkg.dependencies, ...pkg.devDependencies };
        let tech = "Node.js";
        if (deps.next) tech = "Next.js";
        else if (deps.react) tech = "React";
        else if (deps.vue) tech = "Vue";
        else if (deps.svelte) tech = "Svelte";
        else if (deps["@angular/core"]) tech = "Angular";

        components.push({
          name: dir.charAt(0).toUpperCase() + dir.slice(1),
          type: "frontend",
          path: dir,
          tech,
          icon: "⚛️",
        });
      } catch { /* skip */ }
      break;
    }
  }

  // Detect Chrome extension
  for (const dir of ["chrome-extension", "extension", "ext"]) {
    if (hasFile(dir, "manifest.json")) {
      components.push({
        name: "Chrome Extension",
        type: "extension",
        path: dir,
        tech: "Chrome MV3",
        icon: "🧩",
      });
      break;
    }
  }

  // Detect .claude config
  if (hasFile(".claude", "settings.json") || hasFile("", "CLAUDE.md")) {
    components.push({
      name: "Claude Config",
      type: "config",
      path: ".claude",
      tech: "Claude Code",
      icon: "🤖",
    });
  }

  // Detect Docker
  if (hasFile("", "Dockerfile") || hasFile("", "docker-compose.yml") || hasFile("backend", "Dockerfile")) {
    components.push({
      name: "Docker",
      type: "docker",
      path: ".",
      tech: "Docker / Compose",
      icon: "🐳",
    });
  }

  // Detect CI
  if (hasFile(".github/workflows", "ci.yml") || hasFile(".github/workflows", "ci.yaml")) {
    components.push({
      name: "CI/CD",
      type: "ci",
      path: ".github/workflows",
      tech: "GitHub Actions",
      icon: "⚡",
    });
  }

  // Detect YAML config directory
  for (const dir of ["config", "conf", "settings"]) {
    try {
      const dirPath = path.join(rootDir, dir);
      if (fs.existsSync(dirPath) && fs.statSync(dirPath).isDirectory()) {
        const yamlFiles = fs.readdirSync(dirPath).filter((f) => f.endsWith(".yaml") || f.endsWith(".yml") || f.endsWith(".json"));
        if (yamlFiles.length > 0) {
          components.push({
            name: "Config",
            type: "config",
            path: dir,
            tech: `YAML/JSON (${yamlFiles.length} files)`,
            icon: "⚙️",
          });
          break;
        }
      }
    } catch { /* skip */ }
  }

  // Detect knowledge / data layer
  for (const dir of ["knowledge", "data", "models"]) {
    try {
      const dirPath = path.join(rootDir, dir);
      if (fs.existsSync(dirPath) && fs.statSync(dirPath).isDirectory()) {
        const files = fs.readdirSync(dirPath);
        if (files.length > 0) {
          components.push({
            name: dir.charAt(0).toUpperCase() + dir.slice(1),
            type: "database",
            path: dir,
            tech: "Data / Knowledge",
            icon: "🗄️",
          });
          break;
        }
      }
    } catch { /* skip */ }
  }

  return components;
}

module.exports = { scanProject, detectComponents, getCategory, getLanguage };
