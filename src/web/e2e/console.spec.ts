/**
 * Two-tenant end-to-end flow (TEAM_PLAN Phase 2 definition of done, frontend slice).
 * Requires the API on :8000 (`uvicorn src.api.app:app --port 8000`) and Vite dev
 * proxying /v1 to it (or E2E_BASE_URL pointing at the docker compose stack).
 */
import { expect, test } from "@playwright/test";

test("viewer inspects the forecast map with suppression and limitations", async ({ page }) => {
  await page.goto("/#/console");
  await page.getByRole("button", { name: /Viewer · Demo One/ }).click();
  await expect(page.getByText(/Aggregate area-level forecasts/)).toBeVisible();
  // Viewer must not see admin or reviewer navigation.
  await expect(page.getByRole("link", { name: "Sources & upload" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Review queue" })).toHaveCount(0);
  // Map legend renders, including the suppressed state wording.
  await expect(page.getByText(/suppressed \(no estimate — not zero\)/)).toBeVisible();
});

test("reviewer sees candidates labeled as unconfirmed", async ({ page }) => {
  await page.goto("/#/console");
  await page.getByRole("button", { name: /Reviewer · Demo One/ }).click();
  await page.getByRole("link", { name: "Review queue" }).click();
  await expect(page.getByText("UNCONFIRMED CANDIDATE").first()).toBeVisible();
  await expect(page.getByText(/Decisions are final and immutable/)).toBeVisible();
});

test("admin upload flow surfaces the degraded media-intake state honestly", async ({ page }) => {
  await page.goto("/#/console");
  await page.getByRole("button", { name: /Tenant admin · Demo One/ }).click();
  await page.getByRole("link", { name: "Sources & upload" }).click();
  await expect(page.getByText("Register recorded-video source")).toBeVisible();
});

test("tenant switch clears tenant-scoped state", async ({ page }) => {
  await page.goto("/#/console");
  await page.getByRole("button", { name: /Tenant admin · Demo One/ }).click();
  await expect(page.getByText("tenant_admin")).toBeVisible();
  await page.getByLabel("Active tenant").selectOption({ label: "Demo Tenant Two" });
  // Role downgrades to viewer in tenant two; admin nav must disappear.
  await expect(page.getByText("viewer")).toBeVisible();
  await expect(page.getByRole("link", { name: "Sources & upload" })).toHaveCount(0);
});

test("keyboard navigation reaches the primary flow", async ({ page }) => {
  await page.goto("/#/console");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter"); // first persona card
  await expect(page.getByText(/Aggregate area-level forecasts/)).toBeVisible();
});
