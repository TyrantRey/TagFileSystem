// `tfs ui` prints `http://host:port/ui/#token=<token>` (DESIGN/v0-5-0.md
// §3.2). A fragment never leaves the browser, so the daemon's log never sees
// the token; the page keeps it in sessionStorage for its API calls and scrubs
// the URL before the router mounts, so neither the address bar nor a copied
// link carries it. A stored token is kept when the fragment has none.

export const TOKEN_KEY = "tfs.token";

/** The parts of `window` the handoff touches — a test passes a fake. */
export interface TokenWindow {
  location: { hash: string; pathname: string; search: string };
  sessionStorage: Pick<Storage, "getItem" | "setItem">;
  history: Pick<History, "replaceState">;
}

export function adoptTokenFromFragment(
  win: TokenWindow = window,
): string | null {
  const match = /^#token=([^&]+)/.exec(win.location.hash);
  const raw = match?.[1];
  if (!raw) {
    adopted = null; // nothing adopted on this load
    return getToken(win);
  }
  const token = decodeURIComponent(raw);
  try {
    win.sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage disabled (private mode, a locked-down profile): the page still
    // works for this load through the module-level copy below.
  }
  adopted = token;
  win.history.replaceState(
    null,
    "",
    `${win.location.pathname}${win.location.search}#/`,
  );
  return token;
}

let adopted: string | null = null;

export function getToken(win: TokenWindow = window): string | null {
  try {
    return win.sessionStorage.getItem(TOKEN_KEY) ?? adopted;
  } catch {
    return adopted;
  }
}
