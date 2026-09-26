import { defineConfig } from "@playwright/test";

// End-to-end UI tests against a running OwnAI stack (dev server or compose).
//   OWNAI_UI_URL=http://localhost:5173 npx playwright test
export default defineConfig({
  testDir: "e2e",
  timeout: Number(process.env.OWNAI_E2E_TIMEOUT_MS ?? 600_000),
  use: {
    baseURL: process.env.OWNAI_UI_URL ?? "http://localhost:5173",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH } : {},
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  reporter: [["list"]],
});
