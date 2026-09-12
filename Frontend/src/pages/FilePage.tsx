import { Link, useSearchParams } from "react-router-dom";

import type { Explain, FileDetail, FileHistory } from "../api/types";
import { useApi } from "../api/useApi";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  PageHeader,
  TagChips,
  When,
} from "../components/bits";
import { Timeline } from "../components/Timeline";
import { bytes, folderOf, short } from "../lib/fmt";

function Explained({ explain }: { explain: Explain }) {
  return (
    <>
      {!explain.known && (
        <p className="muted">
          Not indexed: what applies is read from the name alone.
        </p>
      )}
      {explain.applied.length === 0 && (
        <Empty>Nothing applies to this file.</Empty>
      )}
      {explain.applied.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Handler</th>
              <th>Hooks</th>
              <th>From</th>
            </tr>
          </thead>
          <tbody>
            {explain.applied.map((entry, index) => (
              <tr key={`${entry.file}-${entry.display}-${index}`}>
                <td className="mono">{entry.display}</td>
                <td>{entry.hooks.join(", ") || "–"}</td>
                <td className="mono muted">{entry.file}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {explain.suppressed.length > 0 && (
        <>
          <h2>Suppressed</h2>
          <table>
            <thead>
              <tr>
                <th>Handler</th>
                <th>From</th>
                <th>Excluded by</th>
              </tr>
            </thead>
            <tbody>
              {explain.suppressed.map((entry, index) => (
                <tr key={`${entry.file}-${entry.display}-${index}`}>
                  <td className="mono">{entry.display}</td>
                  <td className="mono muted">{entry.file}</td>
                  <td className="mono">{entry.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {explain.defaults.length > 0 && (
        <>
          <h2>Tagged defaults</h2>
          <table>
            <thead>
              <tr>
                <th>Handler</th>
                <th>Tag</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {explain.defaults.map((d) => (
                <tr key={`${d.script}.${d.handler}-${d.tag}`}>
                  <td className="mono">
                    {d.script}.{d.handler}
                  </td>
                  <td>
                    <TagChips tags={[d.tag]} />
                  </td>
                  <td className="muted">
                    {d.suppressed_by ? (
                      <>
                        suppressed by{" "}
                        <span className="mono">{d.suppressed_by}</span>
                      </>
                    ) : (
                      "applies"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {explain.problems.length > 0 && (
        <>
          <h2>Configuration problems</h2>
          <ul>
            {explain.problems.map((p, index) => (
              <li key={index}>
                <Badge value={p.severity} />{" "}
                <span className="kind">{p.kind}</span> {p.message}
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

export function FilePage() {
  const [search] = useSearchParams();
  const path = search.get("path") ?? "";
  const detail = useApi<FileDetail>("/file", { path, deleted: true });
  const explain = useApi<Explain>("/file/explain", { path });
  const history = useApi<FileHistory>("/file/history", { path });
  const f = detail.data;

  if (!path) {
    return (
      <div className="banner banner-warn">
        No file named: open one from the Files page.
      </div>
    );
  }
  return (
    <>
      <PageHeader
        title={<span className="path">{path}</span>}
        onRefresh={() => {
          detail.reload();
          explain.reload();
          history.reload();
        }}
        loading={detail.loading}
      >
        <Link
          to={`/files?prefix=${encodeURIComponent(folderOf(path))}`}
          className="muted"
        >
          in {folderOf(path)}
        </Link>
      </PageHeader>
      {detail.error && <ErrorBox error={detail.error} />}
      {!f && !detail.error && <Loading />}
      {f && (
        <div className="panel">
          <dl className="fields">
            <dt>Tags</dt>
            <dd>
              <TagChips tags={f.tags} />
            </dd>
            <dt>Status</dt>
            <dd>
              <Badge value={f.status === "deleted" ? "muted" : "ok"} />{" "}
              {f.status}
            </dd>
            <dt>Size</dt>
            <dd>{bytes(f.size)}</dd>
            <dt>Type</dt>
            <dd>
              {f.mime_type ?? "–"}{" "}
              <span className="muted">{f.format ?? ""}</span>
            </dd>
            <dt>Hash</dt>
            <dd className="mono" title={f.hash}>
              {short(f.hash)}
            </dd>
            <dt>Added</dt>
            <dd>
              <When iso={f.added} />
            </dd>
            <dt>File id</dt>
            <dd className="mono muted">{f.file_id}</dd>
          </dl>
        </div>
      )}

      <h2>What applies</h2>
      {explain.error && <ErrorBox error={explain.error} />}
      {explain.data && <Explained explain={explain.data} />}

      <h2>History</h2>
      {history.error && history.error.status !== 404 && (
        <ErrorBox error={history.error} />
      )}
      {history.error && history.error.status === 404 && (
        <Empty>Not indexed: no history yet.</Empty>
      )}
      {history.data && (
        <Timeline entries={[...history.data.timeline].reverse()} />
      )}
    </>
  );
}
