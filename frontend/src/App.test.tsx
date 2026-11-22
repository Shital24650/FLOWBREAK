import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe("FLOWBREAK frontend", () => {
  it("loads and renders run history from the backend API", async () => {
    const sampleRun = {
      id: "run-1",
      url: "https://example.com",
      status: "passed",
      is_demo: false,
      started_at: "2026-11-22T07:00:00Z",
      viewports: [],
      failures: [],
      links: [],
      javascript_errors: [],
      http_errors: [],
      screenshots: [],
      metrics: { failures: 0 },
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [sampleRun],
    });
    vi.stubGlobal("fetch", fetchMock);

    await act(async () => {
      root.render(<App />);
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/runs",
      expect.objectContaining({ headers: { "Content-Type": "application/json" } }),
    );
    expect(container.textContent).toContain("Reliability overview");
    expect(container.textContent).toContain("example.com");
    expect(container.textContent).toContain("passed");
  });

  it("shows an API error instead of silently presenting a connected state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("API unavailable")));

    await act(async () => {
      root.render(<App />);
    });

    expect(container.textContent).toContain("API unavailable");
  });
});
