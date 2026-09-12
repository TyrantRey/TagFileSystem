import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { TimelineEntry } from "../api/types";
import { Timeline } from "./Timeline";

const ENTRIES: TimelineEntry[] = [
  {
    at: "2026-09-12T10:02:00+00:00",
    kind: "problem",
    record: {
      id: "p1",
      severity: "info",
      kind: "run.ok",
      message: "photo finished",
      action_name: "photo",
      file_id: "f1",
      run_id: "r1",
      occurred_at: "2026-09-12T10:02:00+00:00",
      delivered_at: null,
    },
  },
  {
    at: "2026-09-12T10:01:00+00:00",
    kind: "provenance",
    record: {
      file_id: "f2",
      run_id: "r1",
      kind: "emitted",
      ambiguous: false,
      created_at: "2026-09-12T10:01:00+00:00",
    },
  },
  {
    at: "2026-09-12T10:00:00+00:00",
    kind: "run",
    record: {
      id: "r1",
      action_id: "a1",
      action_name: "photo",
      handler: "resize",
      hook: "added",
      file_id: "f1",
      file_hash: "abc",
      slug: "photo.resize(width=800)",
      args: { width: 800 },
      result: null,
      status: "ok",
      error: null,
      source: "watch",
      parent_run_id: null,
      retry_of: null,
      started_at: "2026-09-12T10:00:00+00:00",
      finished_at: "2026-09-12T10:00:01+00:00",
      code_version: "0.5.0",
      code_hash: null,
    },
  },
  {
    at: "2026-09-12T09:59:00+00:00",
    kind: "event",
    record: {
      id: "e1",
      name: "file_added",
      description: null,
      file_id: "f1",
      tag_id: null,
      tag_name: "trip",
      occurred_at: "2026-09-12T09:59:00+00:00",
    },
  },
];

describe("Timeline", () => {
  it("renders every kind in the order given and links runs", () => {
    render(
      <MemoryRouter>
        <Timeline entries={ENTRIES} />
      </MemoryRouter>,
    );

    const items = screen.getAllByRole("listitem");
    expect(items.map((li) => li.className)).toEqual([
      "timeline-problem",
      "timeline-provenance",
      "timeline-run",
      "timeline-event",
    ]);
    expect(
      screen.getByRole("link", { name: "photo.resize(width=800)" }),
    ).toHaveAttribute("href", "/run/r1");
    expect(screen.getByText("emitted")).toHaveClass("badge-ok");
    expect(screen.getByText("run.ok")).toBeInTheDocument();
    expect(screen.getByText("trip")).toHaveClass("chip");
  });

  it("says so when there is nothing", () => {
    render(
      <MemoryRouter>
        <Timeline entries={[]} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Nothing recorded yet.")).toBeInTheDocument();
  });
});
