import { build } from "esbuild";
await build({
  entryPoints: ["src/index.jsx"],
  bundle: true, format: "esm", minify: true, target: "es2020", jsx: "automatic",
  loader: { ".css": "text", ".json": "json" },
  define: { "process.env.NODE_ENV": '"production"' },
  outfile: "../custom_components/bas_wiresheet/frontend/bas-wiresheet-panel.js",
  legalComments: "eof",
});
console.log("built");
