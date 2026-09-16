import { useSearchParams } from "react-router-dom";

import { SEVERITIES, type Page, type Problem } from "../api/types";
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

const LIMIT = 50;

export function ProblemsPage() {
  const [search, setSearch] = useSearchParams();
  const severity = search.get("severity") ?? "";
  const kind = search.get("kind") ?? "";
  const action = search.get("action") ?? "";
  const undelivered = search.get("undelivered") === "1";
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

  const problems = useApi<Page<Problem>>("/problems", {
    severity: severity || undefined,
    kind: kind || undefined,
    action: action || undefined,
    undelivered,
    limit: LIMIT,
    offset,
  });

  return (
    <>
      <PageHeader
        title="Problems"
        onRefresh={problems.reload}
        loading={problems.loading}
      />
      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          update({
            kind: String(form.get("kind") ?? ""),
            action: String(form.get("action") ?? ""),
          });
        }}
      >
        <label>
          at least{" "}
          <select
            value={severity}
            onChange={(event) => update({ severity: event.target.value })}
          >
            <option value="">any severity</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          kind{" "}
          <input
            type="text"
            name="kind"
            defaultValue={kind}
            key={`k${kind}`}
            placeholder="run.failed"
          />
        </label>
        <label>
          action{" "}
          <input
            type="text"
            name="action"
            defaultValue={action}
            key={`a${action}`}
          />
        </label>
        <button type="submit">Filter</button>
        <label>
          <input
            type="checkbox"
            checked={undelivered}
            onChange={(event) =>
              update({ undelivered: event.target.checked ? "1" : null })
            }
          />
          undelivered only
        </label>
      </form>
      {problems.error && <ErrorBox error={problems.error} />}
      {!problems.data && !problems.error && <Loading />}
      {problems.data && problems.data.items.length === 0 && (
        <Empty>No problem matches.</Empty>
      )}
      {problems.data && problems.data.items.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Severity</th>
              <th>Kind</th>
              <th>Message</th>
              <th>Action</th>
              <th>File</th>
              <th>Run</th>
            </tr>
          </thead>
          <tbody>
            {problems.data.items.map((p) => (
              <tr key={p.id}>
                <td>
                  <When iso={p.occurred_at} />
                </td>
                <td>
                  <Badge value={p.severity} />
                </td>
                <td className="kind">{p.kind}</td>
                <td>{p.message}</td>
                <td className="muted">{p.action_name ?? "–"}</td>
                <td>
                  <PathLink path={p.path} />
                </td>
                <td>
                  <RunLink id={p.run_id} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {problems.data && (
        <Pager
          total={problems.data.total}
          limit={problems.data.limit}
          offset={problems.data.offset}
          onPage={(next) => update({ offset: String(next) }, true)}
        />
      )}
    </>
  );
}
