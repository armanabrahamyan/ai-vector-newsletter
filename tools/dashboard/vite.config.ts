// Local-only dashboard server. Two jobs beyond serving the app:
//
// 1. The /gc proxy injects the GoatCounter API token from the repo's .env
//    server-side, so the token never reaches browser JavaScript and the
//    browser never fights CORS.
// 2. ISSUE_NUMBERS: at startup we read every data/released/*/issue.json and
//    inject a { "/released/<date>.html": "No. <n>" } map into the bundle, so
//    the pages table can label issues by number without any runtime lookups.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { config as dotenv } from "dotenv";
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

const here = import.meta.dirname;
const repoRoot = resolve(here, "../..");
dotenv({ path: resolve(repoRoot, ".env") });

const token = process.env.GOATCOUNTER_API_TOKEN;
if (!token) {
  console.warn(
    "\n[dashboard] GOATCOUNTER_API_TOKEN missing from the repo .env — " +
      "API calls will fail. See docs/HANDBOOK.md.\n",
  );
}

function issueNumbers(): Record<string, string> {
  const map: Record<string, string> = {};
  const released = resolve(repoRoot, "data/released");
  if (!existsSync(released)) return map;
  for (const day of readdirSync(released)) {
    const p = resolve(released, day, "issue.json");
    if (!existsSync(p)) continue;
    try {
      const issue = JSON.parse(readFileSync(p, "utf-8"));
      if (issue.issue_number) {
        const rev = issue.revision ? `.${issue.revision}` : "";
        map[`/released/${day}.html`] = `No. ${issue.issue_number}${rev}`;
      }
    } catch {
      /* a malformed archive day never blocks the dashboard */
    }
  }
  return map;
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": resolve(here, "src") },
  },
  define: {
    ISSUE_NUMBERS: JSON.stringify(issueNumbers()),
  },
  server: {
    port: 5199,
    proxy: {
      "/gc": {
        target: "https://aivector.goatcounter.com",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/gc/, ""),
        headers: { Authorization: `Bearer ${token ?? ""}` },
      },
    },
  },
});
