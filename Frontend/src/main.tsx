import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { adoptTokenFromFragment } from "./auth/token";
import "./styles/tokens.css";
import "./styles/app.css";

// Before the router mounts and before any fetch: take the token out of the
// URL (DESIGN/v0-5-0.md §3.2).
adoptTokenFromFragment();

const container = document.getElementById("root");
if (!container) throw new Error("index.html has no #root element");
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
