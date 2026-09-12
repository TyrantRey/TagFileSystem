// The small pieces every page uses.

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { ApiError } from "../api/client";
import type { RunStatus, Severity } from "../api/types";
import { local } from "../lib/fmt";

export function Loading() {
  return <p className="muted">Loading…</p>;
}

export function ErrorBox({ error }: { error: ApiError }) {
  let hint = "";
  if (error.status === 0) hint = " Is the daemon running? `tfs start -d`";
  else if (error.status === 401)
    hint = " Run `tfs ui` and open the address it prints.";
  return (
    <div className="banner banner-err" role="alert">
      {error.status ? `${error.status}: ` : ""}
      {error.message}
      {hint}
    </div>
  );
}

/** A page header with its Refresh button. */
export function PageHeader({
  title,
  onRefresh,
  loading,
  children,
}: {
  title: ReactNode;
  onRefresh?: () => void;
  loading?: boolean;
  children?: ReactNode;
}) {
  return (
    <div className="page-header">
      <h1>{title}</h1>
      {children}
      {onRefresh && (
        <button
          type="button"
          className="refresh"
          onClick={onRefresh}
          disabled={loading}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      )}
    </div>
  );
}

const TONES: Record<string, string> = {
  ok: "ok",
  failed: "err",
  interrupted: "err",
  skipped: "muted",
  queued: "muted",
  running: "info",
  crit: "crit",
  err: "err",
  warn: "warn",
  info: "info",
  emitted: "ok",
  observed: "warn",
};

export function Badge({ value }: { value: RunStatus | Severity | string }) {
  return (
    <span className={`badge badge-${TONES[value] ?? "muted"}`}>{value}</span>
  );
}

export function When({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span className="muted">–</span>;
  return (
    <time dateTime={iso} title={iso}>
      {local(iso)}
    </time>
  );
}

export function fileHref(path: string): string {
  return `/file?path=${encodeURIComponent(path)}`;
}

export function PathLink({ path }: { path: string | null | undefined }) {
  if (!path) return <span className="muted">–</span>;
  return (
    <Link className="path" to={fileHref(path)}>
      {path}
    </Link>
  );
}

export function RunLink({
  id,
  children,
}: {
  id: string | null | undefined;
  children?: ReactNode;
}) {
  if (!id) return <span className="muted">–</span>;
  return (
    <Link className="mono" to={`/run/${encodeURIComponent(id)}`}>
      {children ?? id.slice(0, 8)}
    </Link>
  );
}

export function TagChips({ tags }: { tags: string[] }) {
  if (tags.length === 0) return <span className="muted">–</span>;
  return (
    <span className="chips">
      {tags.map((tag) => (
        <Link
          key={tag}
          className="chip"
          to={`/files?tag=${encodeURIComponent(tag)}`}
        >
          {tag}
        </Link>
      ))}
    </span>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  const text =
    value === undefined ? "undefined" : JSON.stringify(value, null, 2);
  return <pre className="json">{text}</pre>;
}

export function Pager({
  total,
  limit,
  offset,
  onPage,
}: {
  total: number;
  limit: number;
  offset: number;
  onPage: (offset: number) => void;
}) {
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + limit, total);
  return (
    <div className="pager">
      <button
        type="button"
        disabled={offset === 0}
        onClick={() => onPage(Math.max(0, offset - limit))}
      >
        ‹ Previous
      </button>
      <span>
        {first}–{last} of {total}
      </span>
      <button
        type="button"
        disabled={last >= total}
        onClick={() => onPage(offset + limit)}
      >
        Next ›
      </button>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted empty">{children}</p>;
}
