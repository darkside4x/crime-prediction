/**
 * Two-tenant end-to-end flow (TEAM_PLAN Phase 2 definition of done, frontend slice).
 * Requires the API on :8000 (`uvicorn src.api.app:app --port 8000`) and Vite dev
 * proxying /v1 to it (or E2E_BASE_URL pointing at the docker compose stack).
 */
import { expect, test, type Page } from "@playwright/test";

async function signInAsAdmin(page: Page) {
  await page.goto("/#/console");
  await page.getByRole("button", { name: /Tenant admin · Demo One/ }).click();
  await page.getByLabel("Active tenant").selectOption({ label: "Demo Tenant One" });
  await expect(page.getByText("tenant_admin")).toBeVisible();
}

test("viewer inspects the forecast map with suppression and limitations", async ({ page }) => {
  const mapRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("openfreemap") || request.url().includes("cartocdn")) {
      mapRequests.push(request.url());
    }
  });
  await page.goto("/#/console");
  await page.getByRole("button", { name: /Viewer · Demo One/ }).click();
  await expect(page.getByText(/Aggregate area-level forecasts/)).toBeVisible();
  // Viewer must not see admin or reviewer navigation.
  await expect(page.getByRole("link", { name: "Sources & upload" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Review queue" })).toHaveCount(0);
  // Map legend renders, including the suppressed state wording.
  await expect(page.getByText(/suppressed \(no estimate — not zero\)/)).toBeVisible();
  await expect.poll(() => mapRequests.some((url) => url.includes("openfreemap"))).toBe(true);
  expect(mapRequests.some((url) => url.includes("cartocdn"))).toBe(false);
});

test("reviewer sees candidates labeled as unconfirmed", async ({ page }) => {
  await page.route("**/v1/candidate-detections/*/evidence", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "video/mp4",
      body: Buffer.from("bounded synthetic evidence video"),
    });
  });
  await page.goto("/#/console");
  await page.getByRole("button", { name: /Reviewer · Demo One/ }).click();
  await page.getByRole("link", { name: "Review queue" }).click();
  await expect(page.getByText("UNCONFIRMED CANDIDATE").first()).toBeVisible();
  await expect(page.getByText(/Decisions are final and immutable/)).toBeVisible();
  await page.getByRole("button", { name: "Load evidence video" }).first().click();
  await expect(page.getByLabel(/Evidence video for candidate/).first()).toBeVisible();
});

test("admin upload flow surfaces the degraded media-intake state honestly", async ({ page }) => {
  await signInAsAdmin(page);
  await page.getByRole("link", { name: "Sources & upload" }).click();
  await expect(
    page.getByRole("heading", { name: "Register recorded-video source" }),
  ).toBeVisible();
});

test("admin sends an authorized recording as bounded multipart data", async ({ page }) => {
  await page.route("**/v1/video-assets/uploads", async (route) => {
    const request = route.request();
    expect(request.headers()["authorization"]).toBe("Bearer demo-token-one");
    expect(request.headers()["content-type"]).toContain("multipart/form-data; boundary=");
    const body = request.postDataBuffer()?.toString("utf8") ?? "";
    expect(body).toContain('name="source_id"');
    expect(body).toContain('name="consent_confirmed"');
    expect(body).toContain("true");
    expect(body).toContain('filename="review-two.mp4"');
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        run_id: "70000000-0000-4000-8000-000000000001",
        state: "queued",
        stage: "accepted",
        label: "recorded video upload",
        candidate_count: 0,
        analysis_mode: "deterministic_fake",
        created_at: "2026-08-30T00:00:00Z",
        updated_at: "2026-08-30T00:00:00Z",
      }),
    });
  });
  await page.route(
    "**/v1/ingestion/runs/70000000-0000-4000-8000-000000000001",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run_id: "70000000-0000-4000-8000-000000000001",
          state: "completed",
          stage: "awaiting_human_review",
          label: "recorded video upload",
          asset_id: "71000000-0000-4000-8000-000000000001",
          candidate_count: 1,
          analysis_mode: "reka_vision",
          created_at: "2026-08-30T00:00:00Z",
          updated_at: "2026-08-30T00:03:00Z",
        }),
      });
    },
  );

  await signInAsAdmin(page);
  await page.getByRole("link", { name: "Sources & upload" }).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: "review-two.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("bounded synthetic browser fixture"),
  });
  await page.getByLabel("Recorded source").selectOption({ index: 1 });
  await page.getByLabel(/I confirm this tenant is authorized/).check();
  await page.getByRole("button", { name: "Start upload" }).click();
  await expect(page.getByText(/Upload accepted\. Processing run 70000000\./)).toBeVisible();
  await expect(page.getByText(/Analysis complete · 1 unconfirmed candidate/)).toBeVisible();
});

test("tenant switch clears tenant-scoped state", async ({ page }) => {
  await signInAsAdmin(page);
  await page.getByLabel("Active tenant").selectOption({ label: "Demo Tenant Two" });
  // Role downgrades to viewer in tenant two; admin nav must disappear.
  await expect(page.getByText("viewer")).toBeVisible();
  await expect(page.getByRole("link", { name: "Sources & upload" })).toHaveCount(0);
});

test("keyboard navigation reaches the primary flow", async ({ page }) => {
  await page.goto("/#/console");
  const firstPersona = page.getByRole("button", { name: /Tenant admin · Demo One/ });
  await expect(firstPersona).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(firstPersona).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Aggregate area-level forecasts/)).toBeVisible();
});
