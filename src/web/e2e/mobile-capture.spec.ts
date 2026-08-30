import { expect, test, type Page, type Route } from "@playwright/test";

const SOURCE_ID = "10000000-0000-4000-8000-000000000001";
const RUN_ID = "70000000-0000-4000-8000-000000000009";

async function mockAuthenticatedApis(page: Page, role = "tenant_admin") {
  await page.route("**/v1/me/tenants", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        active_tenant_id: "00000000-0000-4000-8000-000000000001",
        tenants: [
          {
            tenant_id: "00000000-0000-4000-8000-000000000001",
            slug: "demo-one",
            display_name: "Demo Tenant One",
            role,
          },
        ],
      }),
    });
  });
  await page.route("**/v1/sources", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            schema_version: "1.0.0",
            tenant_id: "00000000-0000-4000-8000-000000000001",
            source_id: SOURCE_ID,
            name: "Phone demo zone",
            mode: "recorded_video",
            status: "active",
            timezone: "Asia/Kolkata",
            retention_policy_days: 7,
            created_at: "2026-08-30T00:00:00Z",
            updated_at: "2026-08-30T00:00:00Z",
          },
        ],
      }),
    });
  });
  await page.route("**/v1/demo/session/start", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "started", deleted_pending_candidates: 0 }),
    });
  });
}

async function installCameraMock(
  page: Page,
  options: {
    permissionDenied?: boolean;
    mp4Supported?: boolean;
    webmSupported?: boolean;
    recordedBytes?: number;
  } = {},
) {
  await page.addInitScript(
    ({ permissionDenied, mp4Supported, webmSupported, recordedBytes }) => {
      const browserWindow = window as typeof window & { __cameraRequests: number };
      browserWindow.__cameraRequests = 0;

      class FakeVideoTrack extends EventTarget {
        readonly kind = "video";
        readonly label: string;
        readonly deviceId: string;
        enabled = true;
        muted = false;
        readyState: MediaStreamTrackState = "live";

        constructor(deviceId: string, label: string) {
          super();
          this.deviceId = deviceId;
          this.label = label;
        }

        stop() {
          this.readyState = "ended";
        }

        getSettings() {
          return { deviceId: this.deviceId };
        }
      }

      class FakeMediaRecorder {
        static isTypeSupported(mimeType: string) {
          return (
            (Boolean(mp4Supported) && mimeType.startsWith("video/mp4")) ||
            (Boolean(webmSupported) && mimeType.startsWith("video/webm"))
          );
        }

        state: RecordingState = "inactive";
        readonly mimeType: string;
        ondataavailable: ((event: BlobEvent) => void) | null = null;
        onerror: ((event: Event) => void) | null = null;
        onstop: ((event: Event) => void) | null = null;

        constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
          this.mimeType = options?.mimeType ?? "video/webm";
        }

        start() {
          this.state = "recording";
        }

        stop() {
          if (this.state !== "recording") return;
          this.state = "inactive";
          const bytes = recordedBytes
            ? new Uint8Array(recordedBytes)
            : this.mimeType.startsWith("video/mp4")
              ? new Uint8Array([
                  0, 0, 0, 24, 102, 116, 121, 112, 105, 115, 111, 109, 0, 0, 0, 0,
                  105, 115, 111, 109, 97, 118, 99, 49,
                ])
              : new Uint8Array([26, 69, 223, 163, 66, 134, 129, 1]);
          this.ondataavailable?.({
            data: new Blob([bytes], { type: this.mimeType }),
          } as BlobEvent);
          this.onstop?.(new Event("stop"));
        }
      }

      Object.defineProperty(window, "MediaRecorder", {
        configurable: true,
        value: FakeMediaRecorder,
      });
      Object.defineProperty(HTMLMediaElement.prototype, "srcObject", {
        configurable: true,
        get() {
          return (this as HTMLMediaElement & { __stream?: unknown }).__stream ?? null;
        },
        set(value: unknown) {
          (this as HTMLMediaElement & { __stream?: unknown }).__stream = value;
        },
      });
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        value: {
          async getUserMedia(constraints: MediaStreamConstraints) {
            browserWindow.__cameraRequests += 1;
            if (permissionDenied) {
              throw new DOMException("Camera permission denied", "NotAllowedError");
            }
            const exact = (constraints.video as MediaTrackConstraints | undefined)?.deviceId;
            const requested =
              typeof exact === "object" && exact && "exact" in exact ? String(exact.exact) : "rear-camera";
            const track = new FakeVideoTrack(
              requested,
              requested === "front-camera" ? "Front camera" : "Rear camera",
            );
            return {
              getTracks: () => [track],
              getVideoTracks: () => [track],
            } as unknown as MediaStream;
          },
          async enumerateDevices() {
            return [
              { kind: "videoinput", deviceId: "rear-camera", label: "Rear camera", groupId: "" },
              { kind: "videoinput", deviceId: "front-camera", label: "Front camera", groupId: "" },
            ];
          },
        },
      });
    },
    {
      permissionDenied: options.permissionDenied ?? false,
      mp4Supported: options.mp4Supported ?? true,
      webmSupported: options.webmSupported ?? true,
      recordedBytes: options.recordedBytes ?? 0,
    },
  );
}

async function signIn(page: Page) {
  await page.goto("/#/console/mobile-capture");
  await page.getByRole("button", { name: /Tenant admin · Demo One/ }).click();
  await expect(page.getByRole("heading", { name: "MOBILE CAPTURE" })).toBeVisible();
}

test("camera stays off until explicit consent, captures a bounded MP4 and uploads without GPS", async ({
  page,
}) => {
  await page.clock.install();
  await installCameraMock(page);
  await mockAuthenticatedApis(page);

  let uploadBody = "";
  await page.route("**/v1/video-assets/uploads", async (route: Route) => {
    const request = route.request();
    expect(request.headers().authorization).toBe("Bearer demo-token-one");
    expect(request.headers()["content-type"]).toContain("multipart/form-data; boundary=");
    uploadBody = request.postDataBuffer()?.toString("latin1") ?? "";
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        run_id: RUN_ID,
        state: "queued",
        stage: "accepted",
        label: "recorded video upload",
        candidate_count: 0,
        analysis_mode: "reka_vision",
        created_at: "2026-08-30T00:00:00Z",
        updated_at: "2026-08-30T00:00:00Z",
      }),
    });
  });
  await page.route(`**/v1/ingestion/runs/${RUN_ID}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        run_id: RUN_ID,
        state: "completed",
        stage: "awaiting_human_review",
        label: "recorded video upload",
        candidate_count: 1,
        analysis_mode: "reka_vision",
        created_at: "2026-08-30T00:00:00Z",
        updated_at: "2026-08-30T00:00:20Z",
      }),
    });
  });

  await signIn(page);
  expect(await page.evaluate(() => (window as typeof window & { __cameraRequests: number }).__cameraRequests)).toBe(0);

  await page.getByLabel("Registered recorded source").selectOption(SOURCE_ID);
  await page.getByRole("button", { name: "Enable camera preview" }).click();
  await expect(page.getByText(/Camera ready\. Nothing is recorded/)).toBeVisible();
  expect(await page.evaluate(() => (window as typeof window & { __cameraRequests: number }).__cameraRequests)).toBe(1);

  const cameraSelector = page.getByRole("combobox", { name: "Camera", exact: true });
  await cameraSelector.selectOption("front-camera");
  await expect(cameraSelector).toHaveValue("front-camera");
  await page.getByLabel("10s").check();
  await page.getByRole("button", { name: "Start 10-second recording" }).click();
  await expect(page.getByText(/Recording · 10s remaining/)).toBeVisible();
  await page.clock.fastForward(10_000);
  await expect(page.getByText("Bounded clip ready")).toBeVisible();

  await page.getByLabel(/I confirm this tenant is authorized/).check();
  await page.getByRole("button", { name: "Upload for human review" }).click();
  await expect(page.getByText(/Upload accepted · run 70000000/)).toBeVisible();
  await expect(page.getByText(/Analysis complete · 1 unconfirmed candidate/)).toBeVisible();

  expect(uploadBody).toContain(`name="source_id"`);
  expect(uploadBody).toContain(SOURCE_ID);
  expect(uploadBody).toContain(`name="consent_confirmed"`);
  expect(uploadBody).toContain("true");
  expect(uploadBody).toContain("Content-Type: video/mp4");
  expect(uploadBody).toContain("civichalo-mobile-");
  expect(uploadBody.toLowerCase()).not.toContain("latitude");
  expect(uploadBody.toLowerCase()).not.toContain("longitude");
  expect(uploadBody.toLowerCase()).not.toContain("reka_api_key");
});

test("permission denial is explicit and never creates a clip", async ({ page }) => {
  await installCameraMock(page, { permissionDenied: true });
  await mockAuthenticatedApis(page);
  await signIn(page);

  await page.getByRole("button", { name: "Enable camera preview" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /Camera permission was blocked/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Try camera again" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload for human review" })).toBeDisabled();
});

test("a browser without MP4 MediaRecorder support falls back to backend-converted WebM", async ({
  page,
}) => {
  await page.clock.install();
  await installCameraMock(page, { mp4Supported: false, webmSupported: true });
  await mockAuthenticatedApis(page);

  let uploadBody = "";
  await page.route("**/v1/video-assets/uploads", async (route) => {
    uploadBody = route.request().postDataBuffer()?.toString("latin1") ?? "";
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        run_id: RUN_ID,
        state: "queued",
        stage: "accepted",
        label: "recorded video upload",
        candidate_count: 0,
        analysis_mode: "reka_vision",
        created_at: "2026-08-30T00:00:00Z",
        updated_at: "2026-08-30T00:00:00Z",
      }),
    });
  });
  await page.route(`**/v1/ingestion/runs/${RUN_ID}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        run_id: RUN_ID,
        state: "completed",
        stage: "awaiting_human_review",
        label: "recorded video upload",
        candidate_count: 0,
        analysis_mode: "reka_vision",
        created_at: "2026-08-30T00:00:00Z",
        updated_at: "2026-08-30T00:00:20Z",
      }),
    });
  });

  await signIn(page);
  await page.getByLabel("Registered recorded source").selectOption(SOURCE_ID);
  await page.getByRole("button", { name: "Enable camera preview" }).click();
  await page.getByLabel("10s").check();
  await page.getByRole("button", { name: "Start 10-second recording" }).click();
  await page.clock.fastForward(10_000);
  await expect(page.getByText(/WebM · converted securely by the backend/)).toBeVisible();
  await page.getByLabel(/I confirm this tenant is authorized/).check();
  await page.getByRole("button", { name: "Upload for human review" }).click();

  await expect(page.getByText(/Upload accepted · run/)).toBeVisible();
  expect(uploadBody).toContain("Content-Type: video/webm");
  expect(uploadBody).toContain("civichalo-mobile-");
  expect(uploadBody).toContain(".webm");
});

test("a browser without an accepted MediaRecorder format fails closed before camera access", async ({ page }) => {
  await installCameraMock(page, { mp4Supported: false, webmSupported: false });
  await mockAuthenticatedApis(page);
  await signIn(page);

  await expect(page.getByText(/does not offer MP4 or WebM MediaRecorder output/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Enable camera preview" })).toBeDisabled();
  expect(await page.evaluate(() => (window as typeof window & { __cameraRequests: number }).__cameraRequests)).toBe(0);
});

test("an oversized mobile recording is rejected before any upload request", async ({ page }) => {
  await page.clock.install();
  await installCameraMock(page, { recordedBytes: 8 * 1024 * 1024 + 1 });
  await mockAuthenticatedApis(page);
  let uploadRequests = 0;
  await page.route("**/v1/video-assets/uploads", async (route) => {
    uploadRequests += 1;
    await route.abort();
  });

  await signIn(page);
  await page.getByLabel("Registered recorded source").selectOption(SOURCE_ID);
  await page.getByRole("button", { name: "Enable camera preview" }).click();
  await page.getByLabel("10s").check();
  await page.getByRole("button", { name: "Start 10-second recording" }).click();
  await page.clock.fastForward(10_000);

  await expect(page.getByRole("alert").filter({ hasText: /exceeded the secure 8 MB gateway limit/i })).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload for human review" })).toBeDisabled();
  expect(uploadRequests).toBe(0);
});

test("sources shares a clean authenticated route without OAuth or tenant data", async ({ page }) => {
  await mockAuthenticatedApis(page);
  await page.goto("/?code=oauth-code-must-not-leak#/console/sources");
  await page.getByRole("button", { name: /Tenant admin · Demo One/ }).click();

  const sharedLink = page.getByLabel("Mobile capture link");
  await expect(sharedLink).toHaveValue(
    `${new URL(page.url()).origin}/#/console/mobile-capture`,
  );
  await expect(sharedLink).not.toHaveValue(/oauth-code-must-not-leak/);
  await expect(sharedLink).not.toHaveValue(/demo-token-one/);
  await expect(sharedLink).not.toHaveValue(/00000000-0000-4000-8000-000000000001/);
  await expect(page.getByRole("link", { name: "Open on this device" })).toHaveAttribute(
    "href",
    "#/console/mobile-capture",
  );
});

test("a registered source renders only its aggregate H3 map location", async ({ page }) => {
  await mockAuthenticatedApis(page);
  await page.route(`**/v1/sources/${SOURCE_ID}/map-location`, async (route) => {
    expect(route.request().headers().authorization).toBe("Bearer demo-token-one");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        source_id: SOURCE_ID,
        source_name: "Phone demo zone",
        cell_id: "8861892581fffff",
        h3_resolution: 8,
        precision: "h3_area",
      }),
    });
  });

  await page.goto("/#/console/sources");
  await page.getByRole("button", { name: /Tenant admin · Demo One/ }).click();
  await page.getByRole("button", { name: "Show map location" }).click();

  await expect(page.getByLabel("Phone demo zone map location")).toBeVisible();
  await expect(page.getByText(/Approximate H3 resolution 8 area/)).toBeVisible();
  await expect(page.locator(".source-location-map .maplibregl-canvas")).toBeVisible();
});

test("server-resolved viewer role cannot mount the camera route", async ({ page }) => {
  await installCameraMock(page);
  await mockAuthenticatedApis(page, "viewer");
  await page.goto("/#/console/mobile-capture");
  await page.getByRole("button", { name: /Tenant admin · Demo One/ }).click();

  await expect(page.getByRole("heading", { name: "Not available for your role" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Mobile capture" })).toHaveCount(0);
  expect(await page.evaluate(() => (window as typeof window & { __cameraRequests: number }).__cameraRequests)).toBe(0);
});
