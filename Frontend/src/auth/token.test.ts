import { describe, expect, it, vi } from "vitest";

import {
  adoptTokenFromFragment,
  getToken,
  TOKEN_KEY,
  type TokenWindow,
} from "./token";

function fakeWindow(
  hash: string,
  stored: string | null = null,
): TokenWindow & {
  store: Map<string, string>;
} {
  const store = new Map<string, string>();
  if (stored !== null) store.set(TOKEN_KEY, stored);
  return {
    store,
    location: { hash, pathname: "/ui/", search: "" },
    sessionStorage: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => {
        store.set(key, value);
      },
    },
    history: { replaceState: vi.fn() },
  };
}

describe("the token handoff", () => {
  it("takes the token out of the fragment and scrubs the URL", () => {
    const win = fakeWindow("#token=abc%2F123");

    expect(adoptTokenFromFragment(win)).toBe("abc/123");

    expect(win.store.get(TOKEN_KEY)).toBe("abc/123");
    expect(win.history.replaceState).toHaveBeenCalledWith(null, "", "/ui/#/");
    expect(getToken(win)).toBe("abc/123");
  });

  it("keeps a stored token when the fragment has none", () => {
    const win = fakeWindow("#/files", "kept");

    expect(adoptTokenFromFragment(win)).toBe("kept");
    expect(win.history.replaceState).not.toHaveBeenCalled();
  });

  it("has nothing without a fragment or a stored token", () => {
    const win = fakeWindow("");
    expect(adoptTokenFromFragment(win)).toBeNull();
  });

  it("survives a storage that throws", () => {
    const win = fakeWindow("#token=t");
    win.sessionStorage = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };

    expect(adoptTokenFromFragment(win)).toBe("t");
    expect(getToken(win)).toBe("t"); // the module-level copy
  });
});
