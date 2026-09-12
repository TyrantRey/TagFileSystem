import { HashRouter, Route, Routes } from "react-router-dom";

import { getToken } from "./auth/token";
import { Layout } from "./components/Layout";
import { AddonsPage } from "./pages/AddonsPage";
import { FilePage } from "./pages/FilePage";
import { FilesPage } from "./pages/FilesPage";
import { FunctionsPage } from "./pages/FunctionsPage";
import { ProblemsPage } from "./pages/ProblemsPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { StatusPage } from "./pages/StatusPage";

function NoToken() {
  return (
    <main className="main">
      <h1>TagFileSystem</h1>
      <div className="banner banner-warn">
        <p>
          No token for this daemon. Run <code>tfs ui</code> (or{" "}
          <code>tfs ui --open</code>) next to the root and open the address it
          prints: the token rides in the URL fragment and this page keeps it for
          the session.
        </p>
      </div>
    </main>
  );
}

export default function App() {
  if (!getToken()) return <NoToken />;
  return (
    <HashRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<StatusPage />} />
          <Route path="/files" element={<FilesPage />} />
          <Route path="/file" element={<FilePage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/run/:id" element={<RunPage />} />
          <Route path="/problems" element={<ProblemsPage />} />
          <Route path="/addons" element={<AddonsPage />} />
          <Route path="/functions" element={<FunctionsPage />} />
          <Route
            path="*"
            element={
              <div className="banner banner-warn">There is no such page.</div>
            }
          />
        </Routes>
      </Layout>
    </HashRouter>
  );
}
