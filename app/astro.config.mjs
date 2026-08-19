// @ts-check
import { defineConfig } from "astro/config";
import react from "@astrojs/react";
import node from "@astrojs/node";
import benchApi from "./server/api/index.mjs";

// The studio is a local instrument: one process, no hosting, no login. The node
// adapter runs it on your own machine; hosting and shareable links are Phase 4
// and deliberately absent here.
export default defineConfig({
  // Server-rendered, in one process. A stored run is reachable the moment it is
  // written; a statically generated route would have needed the studio
  // restarted before a run it had just produced became visible, which for a
  // tool you run tests from is a bug wearing a build strategy's clothes.
  output: "server",
  adapter: node({ mode: "standalone" }),
  integrations: [react(), benchApi()],
  server: { port: 4321, host: false },
  devToolbar: { enabled: false },
  vite: {
    server: {
      // project/runs/ sits outside app/, which is correct -- one home per
      // object, and the engine owns it. The API reads it with node:fs, never
      // through Vite, so no fs.allow entry is needed or wanted.
      watch: { ignored: ["**/project/runs/**", "**/harness/out/**"] },
    },
  },
});
