import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TagsResult } from "../api/types";
import { TagEditor } from "./TagEditor";

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }));
vi.mock("../api/client", async (original) => ({
  ...(await original<typeof import("../api/client")>()),
  apiPost: postMock,
}));

function result(overrides: Partial<TagsResult>): TagsResult {
  return {
    path: "2024--trip/a.jpg",
    file: {
      path: "2024--trip/a.jpg",
      file_id: "f1",
      hash: "h",
      status: "active",
      tags: ["trip"],
      size: 1,
      mime_type: null,
      added: null,
    },
    added: [],
    removed: [],
    kept: [],
    applies: [],
    ...overrides,
  };
}

describe("TagEditor", () => {
  afterEach(() => {
    cleanup();
    postMock.mockReset();
  });

  it("removes only the tags the name does not spell, and adds", async () => {
    postMock.mockResolvedValueOnce(result({ removed: ["hot"] }));
    const onChanged = vi.fn();
    render(
      <MemoryRouter>
        <TagEditor
          path="2024--trip/a.jpg"
          tags={["trip", "hot"]}
          nameTags={["trip"]}
          onChanged={onChanged}
        />
      </MemoryRouter>,
    );

    expect(screen.queryByLabelText("remove tag trip")).toBeNull();
    fireEvent.click(screen.getByLabelText("remove tag hot"));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(postMock).toHaveBeenCalledWith(
      "/file/tags",
      { path: "2024--trip/a.jpg" },
      { remove: ["hot"] },
    );
    expect(screen.getByText("removed hot")).toBeInTheDocument();

    postMock.mockResolvedValueOnce(
      result({ added: ["wip", "x"], applies: ["photo.resize(width=800)"] }),
    );
    fireEvent.change(screen.getByLabelText("tag to add"), {
      target: { value: "wip, x" },
    });
    fireEvent.click(screen.getByText("Add"));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(2));
    expect(postMock).toHaveBeenLastCalledWith(
      "/file/tags",
      { path: "2024--trip/a.jpg" },
      { add: ["wip", "x"] },
    );
    expect(
      screen.getByText("added wip, x; applies: photo.resize(width=800)"),
    ).toBeInTheDocument();
  });

  it("shows the daemon's refusal", async () => {
    const { ApiError } = await import("../api/client");
    postMock.mockRejectedValueOnce(new ApiError(400, "tag 'a/b': illegal"));
    render(
      <MemoryRouter>
        <TagEditor
          path="a.txt"
          tags={[]}
          nameTags={[]}
          onChanged={() => undefined}
        />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("tag to add"), {
      target: { value: "a/b" },
    });
    fireEvent.click(screen.getByText("Add"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "tag 'a/b': illegal",
    );
  });
});
