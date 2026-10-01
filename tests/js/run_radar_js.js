// Запуск тестов логики радара в Node (без браузера): node tests/js/run_radar_js.js
// Печатает JSON [{name, ok, error, note}], код выхода 1 — если есть провалы.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const js = path.join(__dirname, "..", "..", "albion_trader", "static", "js");
const src = ["radar.js", "radar-selftest.js"].map((f) => fs.readFileSync(path.join(js, f), "utf8")).join("\n;\n");
const store = {};
const ctx = {
  console, Math, JSON, Date, Map, Set, Number, String, Object, Array, Error, Infinity, NaN, isFinite, parseInt,
  loadSettings: (k) => ({ ...(store[k] || {}) }),
  saveSettings: (v, k) => { store[k] = v; },
  esc: (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]),
  fmt: (n) => (n == null || Number.isNaN(n) ? "—" : Math.round(n).toLocaleString("ru-RU")),
  fmt1: (n) => (n == null ? "—" : Number(n).toFixed(1)),
  api: async () => ({}), apiPost: async () => ({}),
  window: { addEventListener() {} },
};
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__results = runRadarSelfTest();", ctx, { filename: "radar-selftest" });
const results = ctx.__results;
process.stdout.write(JSON.stringify(results, null, 1));
process.exit(results.every((r) => r.ok) ? 0 : 1);
