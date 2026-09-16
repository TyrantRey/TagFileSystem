import { Link } from "react-router-dom";

import type { Functions } from "../api/types";
import { useApi } from "../api/useApi";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  PageHeader,
} from "../components/bits";
import { short } from "../lib/fmt";
import { ProblemList } from "./AddonsPage";

/** Every .tfsfunctions.yaml the daemon loaded, root first: what each folder
 * (and everything below it) asks for. */
export function FunctionsPage() {
  const functions = useApi<Functions>("/functions");
  const d = functions.data;
  return (
    <>
      <PageHeader
        title="Functions"
        onRefresh={functions.reload}
        loading={functions.loading}
      />
      {functions.error && <ErrorBox error={functions.error} />}
      {!d && !functions.error && <Loading />}
      {d && d.files.length === 0 && (
        <Empty>
          No .tfsfunctions.yaml loaded: the daemon runs only the tagged
          defaults.
        </Empty>
      )}
      {d &&
        d.files.map((file) => (
          <div className="file-block" key={file.folder}>
            <h2>
              <span className="mono">{file.file}</span>{" "}
              <Link
                className="muted"
                to={`/files?prefix=${encodeURIComponent(file.folder)}`}
              >
                files below
              </Link>{" "}
              {file.digest && (
                <span className="mono muted">({short(file.digest)})</span>
              )}
            </h2>
            {file.error && (
              <div className="banner banner-err">
                <span className="kind">{file.error.kind}</span>{" "}
                {file.error.message} — the previous good version stays loaded.
              </div>
            )}
            {file.entries.length === 0 && file.invalid.length === 0 && (
              <Empty>No entry: nothing applies from this folder.</Empty>
            )}
            {file.entries.length > 0 && (
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Handler</th>
                    <th>Exclude</th>
                    <th>State</th>
                  </tr>
                </thead>
                <tbody>
                  {file.entries.map((entry) => (
                    <tr key={`${entry.order}-${entry.display}`}>
                      <td className="num muted">{entry.order + 1}</td>
                      <td className="mono">{entry.display}</td>
                      <td className="mono">
                        {entry.exclude || <span className="muted">–</span>}
                      </td>
                      <td>
                        {entry.valid ? (
                          <Badge value="ok" />
                        ) : (
                          <>
                            <Badge value="err" />{" "}
                            {entry.problem && (
                              <>
                                <span className="kind">
                                  {entry.problem.kind}
                                </span>{" "}
                                {entry.problem.message}
                              </>
                            )}
                          </>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {file.invalid.length > 0 && (
              <ul>
                {file.invalid.map((item, index) => (
                  <li key={index}>
                    <Badge value="err" />{" "}
                    <span className="mono">{item.ref}</span>:{" "}
                    <span className="kind">{item.kind}</span> {item.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      {d && (
        <>
          <h2>Problems</h2>
          <ProblemList problems={d.problems} />
        </>
      )}
    </>
  );
}
