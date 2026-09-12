import { Link } from "react-router-dom";

import type { Status, Upgrades } from "../api/types";
import { useApi } from "../api/useApi";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  PageHeader,
  RunLink,
  When,
} from "../components/bits";
import { short } from "../lib/fmt";

export function StatusPage() {
  const status = useApi<Status>("/status");
  const upgrades = useApi<Upgrades>("/upgrades", { limit: 10 });
  const s = status.data;
  return (
    <>
      <PageHeader
        title="Status"
        onRefresh={() => {
          status.reload();
          upgrades.reload();
        }}
        loading={status.loading}
      />
      {status.error && <ErrorBox error={status.error} />}
      {!s && !status.error && <Loading />}
      {s && (
        <div className="panel">
          <dl className="fields">
            <dt>Root</dt>
            <dd className="mono">{s.root}</dd>
            <dt>Daemon</dt>
            <dd>
              <Badge value={s.status} /> pid {s.pid}
              {s.started ? "" : " (starting)"}
            </dd>
            <dt>Version</dt>
            <dd>
              {s.version ?? "?"}{" "}
              <span className="mono muted">({short(s.hash)})</span>, API {s.api}
            </dd>
            <dt>Add-ons</dt>
            <dd>
              {s.addons.length === 0 ? (
                <span className="muted">none loaded</span>
              ) : (
                <Link to="/addons">{s.addons.join(", ")}</Link>
              )}
            </dd>
            <dt>Functions</dt>
            <dd>
              <Link to="/functions">
                {s.functions.files} file{s.functions.files === 1 ? "" : "s"}
              </Link>
              {s.functions.problems > 0 && (
                <>
                  , <Badge value="err" /> {s.functions.problems} problem
                  {s.functions.problems === 1 ? "" : "s"}
                </>
              )}
            </dd>
            <dt>In flight</dt>
            <dd>
              {s.in_flight.length === 0 ? (
                <span className="muted">no run in progress</span>
              ) : (
                s.in_flight.map((id) => (
                  <span key={id}>
                    <RunLink id={id} />{" "}
                  </span>
                ))
              )}
            </dd>
            <dt>Web UI</dt>
            <dd>
              {s.ui.built ? "built" : "not built"}
              {s.ui.dist && <span className="mono muted"> {s.ui.dist}</span>}
            </dd>
          </dl>
        </div>
      )}

      <h2>Upgrades</h2>
      {upgrades.error && <ErrorBox error={upgrades.error} />}
      {upgrades.data && upgrades.data.items.length === 0 && (
        <Empty>No self-update recorded for this root.</Empty>
      )}
      {upgrades.data && upgrades.data.items.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Finished</th>
              <th>From</th>
              <th>To</th>
              <th>Schema</th>
              <th>Tests</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {upgrades.data.items.map((u) => (
              <tr key={u.id}>
                <td>
                  <When iso={u.finished_at} />
                </td>
                <td className="mono">
                  {u.from_tag ?? "?"} ({short(u.from_hash)})
                </td>
                <td className="mono">
                  {u.to_tag} ({short(u.to_hash)})
                </td>
                <td>
                  {u.schema_before} → {u.schema_after}
                </td>
                <td>
                  {u.tests_run === null ? (
                    <span className="muted">skipped</span>
                  ) : (
                    `${u.tests_passed ?? 0}/${u.tests_run}`
                  )}
                </td>
                <td>
                  <Badge value={u.outcome === "ok" ? "ok" : "err"} />{" "}
                  {u.outcome !== "ok" && u.outcome}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
