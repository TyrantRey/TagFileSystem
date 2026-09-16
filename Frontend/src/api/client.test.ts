import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TOKEN_KEY } from "../auth/token";
import {
  api,
  ApiError,
  apiBlob,
  apiPost,
  apiUpload,
  buildQuery,
  dispositionName,
  isTokenRejected,
  resetTokenRejected,
} from "./client";

function reply(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("buildQuery", () => {
  it("encodes the way ControlClient does", () => {
    expect(
      buildQuery({
        tag: ["a", "b c"],
        newest: true,
        deleted: false,
        name: undefined,
        prefix: null,
        limit: 50,
        path: "x/y.txt",
      }),
    ).toBe("?tag=a&tag=b+c&newest=1&limit=50&path=x%2Fy.txt");
    expect(buildQuery()).toBe("");
    expect(buildQuery({ only: false })).toBe("");
  });
});

describe("api", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    window.sessionStorage.setItem(TOKEN_KEY, "secret");
    resetTokenRejected();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
    fetchMock.mockReset();
  });

  it("sends the bearer token and the query, and returns the JSON", async () => {
    fetchMock.mockResolvedValueOnce(reply(200, { items: [], total: 0 }));

    const result = await api<{ total: number }>("/files", {
      tag: ["a"],
      limit: 10,
    });

    expect(result.total).toBe(0);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/v1/files?tag=a&limit=10");
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer secret");
    expect(headers["Accept"]).toBe("application/json");
  });

  it("raises the daemon's error message with its status", async () => {
    fetchMock.mockResolvedValueOnce(
      reply(404, { error: "no such file: a.txt" }),
    );

    await expect(api("/file", { path: "a.txt" })).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      message: "no such file: a.txt",
    });
    expect(isTokenRejected()).toBe(false);
  });

  it("flags a rejected token", async () => {
    fetchMock.mockResolvedValueOnce(
      reply(401, { error: "missing or wrong token" }),
    );

    await expect(api("/status")).rejects.toBeInstanceOf(ApiError);
    expect(isTokenRejected()).toBe(true);
  });

  it("reports a daemon that does not answer as status 0", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    const failure = await api("/status").catch((e: unknown) => e);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(0);
    expect((failure as ApiError).message).toContain("Failed to fetch");
  });
});

describe("the writes (DESIGN §12)", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    window.sessionStorage.setItem(TOKEN_KEY, "secret");
    resetTokenRejected();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
    fetchMock.mockReset();
  });

  it("apiPost sends a JSON body with the token", async () => {
    fetchMock.mockResolvedValueOnce(reply(200, { added: ["x"] }));

    const result = await apiPost<{ added: string[] }>(
      "/file/tags",
      { path: "a.txt" },
      { add: ["x"] },
    );

    expect(result.added).toEqual(["x"]);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/v1/file/tags?path=a.txt");
    expect((init as RequestInit).method).toBe("POST");
    expect((init as RequestInit).body).toBe('{"add":["x"]}');
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers["Authorization"]).toBe("Bearer secret");
  });

  it("apiUpload sends the file's bytes as the body", async () => {
    fetchMock.mockResolvedValueOnce(reply(200, { created: true }));
    const file = new Blob(["hello"], { type: "text/plain" });

    await apiUpload("/files/upload", { path: "in/a.txt" }, file);

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/v1/files/upload?path=in%2Fa.txt");
    expect((init as RequestInit).body).toBe(file);
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("text/plain");
  });

  it("apiBlob returns the bytes and the name the daemon gave", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("bytes", {
        status: 200,
        headers: {
          "Content-Type": "application/octet-stream",
          "Content-Disposition":
            "attachment; filename=\"a?.txt\"; filename*=UTF-8''a%C3%A9.txt",
        },
      }),
    );

    const { blob, filename } = await apiBlob("/file/content", {
      path: "a.txt",
    });

    expect(await blob.text()).toBe("bytes");
    expect(filename).toBe("aé.txt");
    expect(dispositionName('attachment; filename="plain.txt"')).toBe(
      "plain.txt",
    );
    expect(dispositionName(null)).toBeNull();
  });

  it("a refused write raises the daemon's message", async () => {
    fetchMock.mockResolvedValueOnce(reply(409, { error: "a.txt exists" }));

    await expect(
      apiUpload("/files/upload", { path: "a.txt" }, new Blob(["x"])),
    ).rejects.toMatchObject({ status: 409, message: "a.txt exists" });
  });
});
