import type { ReactNode } from "react";
import { useSyncExternalStore } from "react";
import { NavLink } from "react-router-dom";

import { isTokenRejected, subscribeTokenRejected } from "../api/client";

const PAGES: [string, string][] = [
  ["/", "Status"],
  ["/files", "Files"],
  ["/runs", "Runs"],
  ["/problems", "Problems"],
  ["/addons", "Add-ons"],
  ["/functions", "Functions"],
];

export function Layout({ children }: { children: ReactNode }) {
  const rejected = useSyncExternalStore(
    subscribeTokenRejected,
    isTokenRejected,
  );
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">TagFileSystem</span>
        <nav className="nav">
          {PAGES.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              {label}
            </NavLink>
          ))}
        </nav>
      </header>
      {rejected && (
        <div className="banner banner-err" role="alert">
          The daemon rejected this token — run <code>tfs ui</code> again and
          open the address it prints.
        </div>
      )}
      <main className="main">{children}</main>
    </div>
  );
}
