import type { Addons, LoadProblem, Signature } from "../api/types";
import { useApi } from "../api/useApi";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  PageHeader,
} from "../components/bits";
import { short } from "../lib/fmt";

/** The parameters after `(path, metadata, ctx)`, the way `tfs list` shows
 * them: `name: type = default`, required ones marked. */
export function parameters(signature: Signature): string {
  const properties = signature.properties ?? {};
  const required = new Set(signature.required ?? []);
  const parts = Object.entries(properties).map(([name, property]) => {
    const type = typeof property.type === "string" ? property.type : "any";
    const fallback =
      "default" in property && !required.has(name)
        ? ` = ${JSON.stringify(property.default)}`
        : "";
    return `${name}: ${type}${fallback}`;
  });
  return parts.length === 0 ? "(no parameters)" : parts.join(", ");
}

export function ProblemList({ problems }: { problems: LoadProblem[] }) {
  if (problems.length === 0) return <Empty>No load problem.</Empty>;
  return (
    <ul>
      {problems.map((p, index) => (
        <li key={index}>
          <Badge value={p.severity} /> <span className="kind">{p.kind}</span>
          {p.file && <span className="mono muted"> {p.file}</span>} {p.message}
        </li>
      ))}
    </ul>
  );
}

export function AddonsPage() {
  const addons = useApi<Addons>("/addons");
  const d = addons.data;
  return (
    <>
      <PageHeader
        title="Add-ons"
        onRefresh={addons.reload}
        loading={addons.loading}
      >
        {d && (
          <span className="muted">
            tfs {d.version ?? "?"} ({short(d.hash)})
          </span>
        )}
      </PageHeader>
      {addons.error && <ErrorBox error={addons.error} />}
      {!d && !addons.error && <Loading />}
      {d && d.actions.length === 0 && (
        <Empty>No add-on loaded: put a script in script/.</Empty>
      )}
      {d &&
        d.actions.map((addon) => (
          <div className="file-block" key={addon.name}>
            <h2>
              {addon.name}{" "}
              <span className="mono muted">
                {addon.script} ({short(addon.script_hash)})
              </span>
            </h2>
            {addon.problem_hooks.length > 0 && (
              <p className="muted">
                Problem handlers at:{" "}
                {addon.problem_hooks.map((h) => (
                  <span key={h}>
                    <Badge value={h} />{" "}
                  </span>
                ))}
              </p>
            )}
            {addon.handlers.length === 0 && (
              <Empty>No file or lifecycle handler.</Empty>
            )}
            {addon.handlers.length > 0 && (
              <table>
                <thead>
                  <tr>
                    <th>Handler</th>
                    <th>Hooks</th>
                    <th>Parameters</th>
                  </tr>
                </thead>
                <tbody>
                  {addon.handlers.map((h) => (
                    <tr key={h.name}>
                      <td className="mono">{h.name}</td>
                      <td>{h.hooks.join(", ")}</td>
                      <td className="mono">{parameters(h.signature)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      {d && (
        <>
          <h2>Load problems</h2>
          <ProblemList problems={d.problems} />
        </>
      )}
    </>
  );
}
