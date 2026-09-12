import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Page, Problem } from "../api/types";
import { ProblemsPage } from "./ProblemsPage";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));
vi.mock("../api/client", async (original) => ({
  ...(await original<typeof import("../api/client")>()),
  api: apiMock,
}));

const FIXTURE: Page<Problem> = {
  items: [
    {
      id: "p2",
      severity: "err",
      kind: "run.failed",
      message: "resize raised ValueError",
      action_name: "photo",
      file_id: "f1",
      run_id: "r1",
      occurred_at: "2026-09-12T10:00:00+00:00",
      delivered_at: null,
      path: "2024--trip/a.jpg",
    },
    {
      id: "p1",
      severity: "warn",
      kind: "functions.unbound",
      message: "nope.run: script/nope.py is not loaded",
      action_name: null,
      file_id: null,
      run_id: null,
      occurred_at: "2026-09-12T09:00:00+00:00",
      delivered_at: "2026-09-12T09:00:01+00:00",
      path: null,
    },
  ],
  total: 2,
  limit: 50,
  offset: 0,
};

afterEach(() => apiMock.mockReset());

describe("ProblemsPage", () => {
  it("lists the feed newest first with badges, links and the page range", async () => {
    apiMock.mockResolvedValue(FIXTURE);

    render(
      <MemoryRouter initialEntries={["/problems"]}>
        <ProblemsPage />
      </MemoryRouter>,
    );

    const rows = await screen.findAllByRole("row");
    const [, first, second] = rows; // the header row, then the items
    expect(first).toBeDefined();
    expect(second).toBeDefined();
    expect(within(first!).getByText("err")).toHaveClass("badge-err");
    expect(within(first!).getByText("run.failed")).toBeInTheDocument();
    expect(
      within(first!).getByRole("link", { name: "2024--trip/a.jpg" }),
    ).toHaveAttribute("href", "/file?path=2024--trip%2Fa.jpg");
    expect(within(first!).getByRole("link", { name: "r1" })).toHaveAttribute(
      "href",
      "/run/r1",
    );
    expect(within(second!).getByText("warn")).toHaveClass("badge-warn");
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledWith(
      "/problems",
      expect.objectContaining({ limit: 50, offset: 0, undelivered: false }),
      expect.anything(),
    );
  });

  it("shows the daemon's error", async () => {
    const { ApiError } = await import("../api/client");
    apiMock.mockRejectedValue(new ApiError(0, "the daemon did not answer (x)"));

    render(
      <MemoryRouter>
        <ProblemsPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "did not answer",
    );
  });
});
