import { useSearchParams } from "react-router-dom";

import { RUN_STATUSES, type Page, type Run } from "../api/types";
import { useApi } from "../api/useApi";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  PageHeader,
  Pager,
  PathLink,
  RunLink,
  When,
} from "../components/bits";
import { duration } from "../lib/fmt";

const LIMIT = 50;

export function RunsPage() {
  const [search, setSearch] = useSearchParams();
  const action = search.get("action") ?? "";
  const handler = search.get("handler") ?? "";
  const status = search.get("status") ?? "";
  const path = search.get("path") ?? "";
  const prefix = search.get("prefix") ?? "";
  const offset = Number(search.get("offset") ?? 0) || 0;

  const update = (
    changes: Record<string, string | null>,
    keepOffset = false,
  ) => {
    const next = new URLSearchParams(search);
    if (!keepOffset) next.delete("offset");
    for (const [key, value] of Object.entries(changes)) {
      if (value === null || value === "") next.delete(key);
      else next.set(key, value);
    }
    setSearch(next);
  };

  const runs = useApi<Page<Run>>("/runs", {
    action: action || undefined,
    handler: handler || undefined,
    status: status ? [status] : undefined,
    path: path || undefined,
    prefix: prefix || undefined,
    limit: LIMIT,
    offset,
  });

  return (
    <>
      <PageHeader title="Runs" onRefresh={runs.reload} loading={runs.loading} />
      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          update({
            action: String(form.get("action") ?? ""),
            handler: String(form.get("handler") ?? ""),
            path: String(form.get("path") ?? ""),
            prefix: String(form.get("prefix") ?? ""),
          });
        }}
      >
        <label>
          action{" "}
          <input
            type="text"
            name="action"
            defaultValue={action}
            key={`a${action}`}
          />
        </label>
        <label>
          handler{" "}
          <input
            type="text"
            name="handler"
            defaultValue={handler}
            key={`h${handler}`}
          />
        </label>
        <label>
          file{" "}
          <input
            type="text"
            name="path"
            defaultValue={path}
            key={`p${path}`}
            placeholder="root-relative path"
          />
        </label>
        <label>
          under{" "}
          <input
            type="text"
            name="prefix"
            defaultValue={prefix}
            key={`u${prefix}`}
            placeholder="folder"
          />
        </label>
        <label>
          status{" "}
          <select
            value={status}
            onChange={(event) => update({ status: event.target.value })}
          >
            <option value="">any</option>
            {RUN_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">Filter</button>
      </form>
      {runs.error && <ErrorBox error={runs.error} />}
      {!runs.data && !runs.error && <Loading />}
      {runs.data && runs.data.items.length === 0 && (
        <Empty>No run matches.</Empty>
      )}
      {runs.data && runs.data.items.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Started</th>
              <th>Handler</th>
              <th>Hook</th>
              <th>Status</th>
              <th>File</th>
              <th>Took</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.items.map((r) => (
              <tr key={r.id}>
                <td>
                  <When iso={r.started_at} />
                </td>
                <td>
                  <RunLink id={r.id}>{r.slug}</RunLink>
                </td>
                <td className="muted">{r.hook}</td>
                <td>
                  <Badge value={r.status} />
                </td>
                <td>
                  {r.path ? (
                    <PathLink path={r.path} />
                  ) : (
                    <span className="muted">{r.source}</span>
                  )}
                </td>
                <td className="num">{duration(r.started_at, r.finished_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {runs.data && (
        <Pager
          total={runs.data.total}
          limit={runs.data.limit}
          offset={runs.data.offset}
          onPage={(next) => update({ offset: String(next) }, true)}
        />
      )}
    </>
  );
}
