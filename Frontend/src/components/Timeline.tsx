import type { TimelineEntry } from "../api/types";
import { Badge, RunLink, When } from "./bits";

function Summary({ entry }: { entry: TimelineEntry }) {
  switch (entry.kind) {
    case "event": {
      const record = entry.record;
      return (
        <>
          <span className="kind">{record.name}</span>
          {record.tag_name && <span className="chip">{record.tag_name}</span>}
          {record.description && (
            <span className="muted"> {record.description}</span>
          )}
        </>
      );
    }
    case "run": {
      const record = entry.record;
      return (
        <>
          <RunLink id={record.id}>{record.slug}</RunLink>{" "}
          <Badge value={record.status} />
          <span className="muted"> {record.hook}</span>
          {record.error && <div className="error-text">{record.error}</div>}
        </>
      );
    }
    case "provenance": {
      const record = entry.record;
      return (
        <>
          <Badge value={record.kind} />
          {record.ambiguous && <span className="muted"> ambiguous</span>}
          <span className="muted"> by run </span>
          <RunLink id={record.run_id} />
        </>
      );
    }
    case "problem": {
      const record = entry.record;
      return (
        <>
          <Badge value={record.severity} />{" "}
          <span className="kind">{record.kind}</span>
          <span> {record.message}</span>
          {record.run_id && (
            <span className="muted">
              {" "}
              run <RunLink id={record.run_id} />
            </span>
          )}
        </>
      );
    }
  }
}

/** A file's history, newest first. */
export function Timeline({ entries }: { entries: TimelineEntry[] }) {
  if (entries.length === 0)
    return <p className="muted empty">Nothing recorded yet.</p>;
  return (
    <ol className="timeline">
      {entries.map((entry, index) => (
        <li key={`${entry.kind}-${index}`} className={`timeline-${entry.kind}`}>
          <span className="timeline-when">
            <When iso={entry.at} />
          </span>
          <span className="timeline-kind">{entry.kind}</span>
          <span className="timeline-body">
            <Summary entry={entry} />
          </span>
        </li>
      ))}
    </ol>
  );
}
