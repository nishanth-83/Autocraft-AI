#!/usr/bin/env node
/**
 * Frontend component and API tests using the project's installed TypeScript compiler.
 */

const fs = require("fs");
const path = require("path");

const FRONTEND_DIR = path.resolve(__dirname, "..", "frontend");
const SRC_DIR = path.join(FRONTEND_DIR, "src");
const ts = require(require.resolve("typescript", { paths: [FRONTEND_DIR] }));

function compileTypeScript(module, filePath) {
  const source = fs.readFileSync(filePath, "utf-8");
  const compiled = ts.transpileModule(source, {
    fileName: filePath,
    compilerOptions: {
      esModuleInterop: true,
      jsx: ts.JsxEmit.React,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
    },
  });
  module._compile(compiled.outputText, filePath);
}

require.extensions[".ts"] = compileTypeScript;
require.extensions[".tsx"] = compileTypeScript;

async function main() {
  const testMod = require(path.join(SRC_DIR, "__tests__", "App.test.tsx"));
  const summary = await testMod.runFrontendTests();
  for (const line of summary.results) {
    console.log(line);
  }
  console.log(
    `\nFrontend Test Summary: ${summary.passed} passed, ${summary.failed} failed.`
  );
  if (summary.failed > 0) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
