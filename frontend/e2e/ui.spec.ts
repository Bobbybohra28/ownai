import { expect, test } from "@playwright/test";

const password = "Str0ng-password!";

test("register, browse every page, and get a streamed AI answer", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("/login");
  await page.getByText("Create one").click();
  await page.fill("#name", "E2E");
  await page.fill("#email", `e2e-${Date.now()}@example.com`);
  await page.fill("#password", password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  for (const [path, heading] of [
    ["/models", "Models"], ["/agents", "Agents"], ["/projects", "Projects"], ["/approvals", "Approvals"],
    ["/rag", "RAG / Documents"], ["/memory", "Memory"], ["/evaluation", "Evaluation"], ["/settings", "Settings"],
    ["/admin", "Admin & diagnostics"],
  ]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  }

  await page.goto("/models");
  await expect(page.locator("td .badge-ok").first()).toBeVisible({ timeout: 120_000 }); // at least one real ONLINE model

  await page.goto("/chat");
  await page.getByRole("button", { name: "New chat" }).click();
  await page.getByLabel("Mode").selectOption("quick");
  await page.locator("textarea").fill("In one sentence: what does the Python keyword 'yield' do?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("live-run")).toBeVisible({ timeout: 60_000 }); // live agent progress
  const answer = page.getByTestId("assistant-message").last();
  await expect(answer).toBeVisible({ timeout: 540_000 }); // persisted final message after the run finished
  await expect(page.getByTestId("live-run")).toHaveCount(0);
  await expect(answer).toHaveAttribute("data-status", "final");
  await expect(answer.locator(".markdown")).toContainText(/\w{3,}/);
  await expect(answer.locator(".badge-info").first()).toBeVisible(); // the model that answered
  expect(errors).toEqual([]);
});
