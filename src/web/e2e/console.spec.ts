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
  await page.goto("/#/console");
  await page.getByRole("button", { name: /Viewer · Demo One/ }).click();
  await expect(page.getByRole("link", { name: "Prediction" })).toHaveAttribute("aria-current", "page");
  // Viewer must not see admin or reviewer navigation.
  await expect(page.getByRole("link", { name: "Sources", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Capture", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Response", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Review" })).toHaveCount(0);
  // Map legend renders, including the suppressed state wording.
  await expect(page.getByText(/suppressed \(no estimate — not zero\)/)).toBeVisible();
  await expect(page.getByRole("application", { name: "Aggregate forecast map" })).toBeVisible();
  await expect(page.locator(".maplibregl-canvas")).toBeVisible();
  await expect(page.getByText(/data as of/)).toBeVisible();
});

test("reviewer sees candidates labeled as unconfirmed", async ({ page }) => {
  await page.route("**/v1/candidate-detections?limit=50", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            schema_version: "1.0.0",
            tenant_id: "11111111-1111-4111-8111-111111111111",
            detection_id: "90000000-0000-4000-8000-000000000001",
            source_id: "91000000-0000-4000-8000-000000000001",
            asset_id: "92000000-0000-4000-8000-000000000001",
            occurred_at: "2026-08-30T12:00:04Z",
            received_at: "2026-08-30T12:00:10Z",
            proposed_category: "traffic_safety",
            confidence: 0.81,
            detector_version: "reka-vision:candidate-v2",
            review_status: "awaiting_review",
            expires_at: "2026-09-06T12:00:10Z",
            evidence_available: true,
            record_type: "unconfirmed_candidate_detection",
          },
        ],
      }),
    });
  });
  await page.route("**/v1/candidate-detections/*/evidence", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "video/mp4",
      body: Buffer.from("bounded synthetic evidence video"),
    });
  });
  await page.goto("/#/console");
  await page.getByRole("button", { name: /Reviewer · Demo One/ }).click();
  await page.getByRole("link", { name: "Review" }).click();
  await expect(page.getByText("UNCONFIRMED CANDIDATE").first()).toBeVisible();
  await expect(page.getByText(/Decisions are final and immutable/)).toBeVisible();
  await page.getByRole("button", { name: "Load evidence video" }).first().click();
  await expect(page.getByLabel(/Evidence video for candidate/).first()).toBeVisible();
});

test("landing page demonstrates the complete product before sign-in", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Xecrex — forecasts/i })).toBeVisible();
  await expect(page.getByText("H3 CELLS").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: /From events to evidence/i })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the console" })).toBeVisible();
});

test("admin chooses live, uploaded, or simulated video input", async ({ page }) => {
  await signInAsAdmin(page);
  await expect(page.getByRole("link", { name: "Live" })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("button", { name: "Analyze next 12 seconds" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /Connect live source/ })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: /Upload video/ }).click();
  await expect(page.getByText("Choose MP4 · up to 64 MB")).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload & analyze video" })).toBeVisible();
  await page.getByRole("tab", { name: /Simulated live/ }).click();
  await expect(page.getByRole("button", { name: "Run simulated analysis" })).toBeVisible();
  await expect(page.getByLabel("Synthetic road simulation preview")).toBeVisible();
  await expect(page.getByRole("link", { name: "Sources & upload" })).toHaveCount(0);
});

test("production-disabled capture and historical fallback are represented honestly", async ({ page }) => {
  await page.route("**/ready", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ready",
        deployment_mode: "production",
        reka_chat: "verified",
        reka_vision: "verified",
        video_service: "durable_connected",
        queue: "durable_connected",
        near_live_capture: "disabled",
        forecast_models: "approved_or_historical_fallback",
        forecast_data: "synthetic_demo",
      }),
    });
  });
  await page.route("**/v1/model-card", async (route) => {
    await route.fulfill({
      status: 404,
      contentType: "application/problem+json",
      body: JSON.stringify({
        status: 404,
        code: "approved_model_not_found",
        message: "No approved model is active",
        retryable: false,
      }),
    });
  });

  await signInAsAdmin(page);
  const liveAction = page.getByRole("button", { name: "Public demo capture disabled" });
  await expect(liveAction).toBeVisible();
  await expect(liveAction).toBeDisabled();
  await expect(page.getByText("Public demo camera disabled in production")).toBeVisible();
  await page.getByText("Register tenant camera connector").click();
  await expect(page.getByLabel("Source name")).toBeEnabled();
  await expect(page.getByText(/Registration stores only the tenant-scoped connector reference/)).toBeVisible();

  await page.getByRole("tab", { name: /Simulated live/ }).click();
  await expect(page.getByRole("button", { name: "Generated demo capture disabled" })).toBeDisabled();

  await page.getByRole("link", { name: "Model" }).click();
  await expect(page.getByRole("heading", { name: /Historical baseline/ })).toBeVisible();
  await expect(page.getByText("Safe fallback is active")).toBeVisible();
  await expect(page.getByText("Could not load the model card.")).toHaveCount(0);
});

test("video input rail remains usable on a narrow console", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signInAsAdmin(page);
  const rail = page.getByRole("tablist", { name: "Video input method" });
  await expect(rail).toBeVisible();
  const box = await rail.boundingBox();
  expect(box).not.toBeNull();
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390);
  await page.getByRole("tab", { name: /Upload video/ }).click();
  await expect(page.getByText("Choose MP4 · up to 64 MB")).toBeVisible();
});

test("tenant switch clears tenant-scoped state", async ({ page }) => {
  await signInAsAdmin(page);
  await page.getByLabel("Active tenant").selectOption({ label: "Demo Tenant Two" });
  // Role downgrades to viewer in tenant two; admin nav must disappear.
  await expect(page.getByTitle("Active role in this tenant")).toHaveText("viewer");
  await expect(page.getByRole("link", { name: "Live" })).toHaveCount(0);
});

test("OAuth history restoration synchronizes the rendered console route", async ({ page }) => {
  await signInAsAdmin(page);
  await page.evaluate(() => {
    const oldURL = window.location.href;
    window.history.replaceState(null, "", `${window.location.pathname}#/console/model-card`);
    window.dispatchEvent(
      new HashChangeEvent("hashchange", { oldURL, newURL: window.location.href }),
    );
  });

  await expect(page.getByRole("heading", { name: "Model card", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Model" })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "Live monitor" })).toHaveCount(0);
});

test("keyboard navigation reaches the primary flow", async ({ page }) => {
  await page.goto("/#/console");
  const firstPersona = page.getByRole("button", { name: /Tenant admin · Demo One/ });
  await expect(firstPersona).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(firstPersona).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Area-level estimates with uncertainty/)).toBeVisible();
});
