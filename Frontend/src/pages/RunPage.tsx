import { useParams } from "react-router-dom";

import type { RunDetail } from "../api/types";
import { useApi } from "../api/useApi";
import {
  Badge,
  Empty,
  ErrorBox,
  JsonBlock,
  Loading,
  PageHeader,
  PathLink,
  RunLink,
  When,
} from "../components/bits";
import { duration, short } from "../lib/fmt";

export function RunPage() {
  const { id = "" } = useParams();
  const detail = useApi<RunDetail>("/run", { id });
  const d = detail.data;
  const run = d?.run;
  return (
    <>
      <PageHeader
        title={run ? <span className="mono">{run.slug}</span> : "Run"}
        onRefresh={detail.reload}
        loading={detail.loading}
      >
        {run && <Badge value={run.status} />}
      </PageHeader>
      {detail.error && <ErrorBox error={detail.error} />}
      {!d && !detail.error && <Loading />}
      {d && run && (
        <>
          <div className="panel">
            <dl className="fields">
              <dt>Run id</dt>
              <dd className="mono">{run.id}</dd>
              <dt>Handler</dt>
              <dd className="mono">
                {run.action_name}.{run.handler || "?"}{" "}
                <span className="muted">on {run.hook}</span>
              </dd>
              <dt>File</dt>
              <dd>
                <PathLink path={run.path} />{" "}
                <span className="mono muted" title={run.file_hash}>
                  {run.file_hash ? short(run.file_hash) : ""}
                </span>
              </dd>
              <dt>Source</dt>
              <dd>{run.source}</dd>
              <dt>Started</dt>
              <dd>
                <When iso={run.started_at} />
              </dd>
              <dt>Finished</dt>
              <dd>
                <When iso={run.finished_at} />{" "}
                <span className="muted">
                  ({duration(run.started_at, run.finished_at)})
                </span>
              </dd>
              {run.parent_run_id && (
                <>
                  <dt>Chained from</dt>
                  <dd>
                    <RunLink id={run.parent_run_id} />
                  </dd>
                </>
              )}
              {run.retry_of && (
                <>
                  <dt>Retry of</dt>
                  <dd>
                    <RunLink id={run.retry_of} />
                  </dd>
                </>
              )}
              <dt>Code</dt>
              <dd className="muted">
                {run.code_version ?? "?"} ({short(run.code_hash)})
              </dd>
            </dl>
          </div>

          {run.error && (
            <>
              <h2>Error</h2>
              <pre className="json error-text">{run.error}</pre>
            </>
          )}
          <h2>Arguments</h2>
          <JsonBlock value={run.args} />
          <h2>Result</h2>
          <JsonBlock value={run.result} />

          <h2>Produced</h2>
          {d.produced.length === 0 && (
            <Empty>This run wrote no file the daemon knows of.</Empty>
          )}
          {d.produced.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>File</th>
                  <th>How</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {d.produced.map((p) => (
                  <tr key={p.file_id}>
                    <td>
                      {p.path ? (
                        <PathLink path={p.path} />
                      ) : (
                        <span className="mono muted">{p.file_id}</span>
                      )}
                    </td>
                    <td>
                      <Badge value={p.kind} />
                      {p.ambiguous && <span className="muted"> ambiguous</span>}
                    </td>
                    <td>
                      <When iso={p.created_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h2>Trace</h2>
          {d.trace.length === 0 && <Empty>No trace entries.</Empty>}
          {d.trace.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>When</th>
                  <th>Kind</th>
                  <th>Payload</th>
                </tr>
              </thead>
              <tbody>
                {d.trace.map((t) => (
                  <tr key={t.seq}>
                    <td className="num muted">{t.seq}</td>
                    <td>
                      <When iso={t.ts} />
                    </td>
                    <td className="kind">{t.kind}</td>
                    <td>
                      {typeof t.payload === "string" ? (
                        <span className="mono">{t.payload}</span>
                      ) : (
                        <JsonBlock value={t.payload} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h2>Problems</h2>
          {d.problems.length === 0 && <Empty>None recorded.</Empty>}
          {d.problems.length > 0 && (
            <ul>
              {d.problems.map((p) => (
                <li key={p.id}>
                  <Badge value={p.severity} />{" "}
                  <span className="kind">{p.kind}</span> {p.message}{" "}
                  <span className="muted">
                    <When iso={p.occurred_at} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </>
  );
}
