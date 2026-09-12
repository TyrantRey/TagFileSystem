// The shapes `GET /api/v1/...` answers with (DESIGN/v0-5-0.md §2), written
// by hand against services/api.py — the record models of
// core/interface/action.py dumped as JSON, plus `path` where the API adds it.

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Status {
  status: "ok" | "stopping";
  root: string;
  pid: number;
  started: boolean;
  version: string | null;
  hash: string | null;
  addons: string[];
  in_flight: string[];
  api: string;
  functions: { files: number; problems: number };
  ui: { built: boolean; dist: string | null };
}

export interface FileItem {
  path: string;
  file_id: string;
  hash: string;
  status: string;
  tags: string[];
  size: number | null;
  mime_type: string | null;
  added: string | null;
}

export interface FileDetail extends FileItem {
  format: string | null;
  mtime_ns: number | null;
}

export interface TagItem {
  name: string;
  tag_id: string;
  added: string;
  files: number;
}

export interface Tags {
  items: TagItem[];
  total: number;
}

export type Hook =
  "added" | "modified" | "removed" | "tagged" | "on_start" | "on_stop";
export type RunStatus =
  "queued" | "running" | "ok" | "failed" | "skipped" | "interrupted";
export type Severity = "crit" | "err" | "warn" | "info";

export const RUN_STATUSES: RunStatus[] = [
  "queued",
  "running",
  "ok",
  "failed",
  "skipped",
  "interrupted",
];
export const SEVERITIES: Severity[] = ["crit", "err", "warn", "info"];

export interface Run {
  id: string;
  action_id: string;
  action_name: string;
  handler: string;
  hook: Hook;
  file_id: string | null;
  file_hash: string;
  slug: string;
  args: Record<string, unknown>;
  result: unknown;
  status: RunStatus;
  error: string | null;
  source: string;
  parent_run_id: string | null;
  retry_of: string | null;
  started_at: string;
  finished_at: string | null;
  code_version: string | null;
  code_hash: string | null;
  /** Added by the API where it resolves file ids; absent inside a timeline. */
  path?: string | null;
}

export interface Problem {
  id: string;
  severity: Severity;
  kind: string;
  message: string;
  action_name: string | null;
  file_id: string | null;
  run_id: string | null;
  occurred_at: string;
  delivered_at: string | null;
  path?: string | null;
}

export interface TraceEntry {
  run_id: string;
  seq: number;
  ts: string;
  kind: string;
  payload: unknown;
}

export interface Provenance {
  file_id: string;
  run_id: string;
  kind: "emitted" | "observed";
  ambiguous: boolean;
  created_at: string;
}

export interface Produced {
  path: string | null;
  file_id: string;
  kind: string;
  ambiguous: boolean;
  created_at: string;
}

export interface EventItem {
  id: string;
  name: string;
  description: string | null;
  file_id: string | null;
  tag_id: string | null;
  tag_name: string | null;
  occurred_at: string;
}

export type TimelineEntry = { at: string } & (
  | { kind: "event"; record: EventItem }
  | { kind: "run"; record: Run }
  | { kind: "provenance"; record: Provenance }
  | { kind: "problem"; record: Problem }
);

export interface FileHistory {
  file: FileItem;
  timeline: TimelineEntry[];
  events: EventItem[];
  runs: Run[];
  provenance: Provenance[];
  problems: Problem[];
}

export interface RunDetail {
  run: Run;
  trace: TraceEntry[];
  produced: Produced[];
  problems: Problem[];
}

/** A load problem as `tfs list` shows it: `file` names a .tfsfunctions.yaml. */
export interface LoadProblem {
  severity: Severity;
  kind: string;
  message: string;
  file?: string;
}

export interface ExplainEntry {
  script: string;
  handler: string;
  args: Record<string, unknown>;
  folder: string;
  file: string;
  display: string;
}

export interface Explain {
  path: string;
  tags: string[];
  known: boolean;
  source: string;
  applied: (ExplainEntry & { hooks: string[] })[];
  suppressed: (ExplainEntry & { reason: string })[];
  defaults: {
    script: string;
    handler: string;
    tag: string;
    suppressed_by: string | null;
  }[];
  problems: LoadProblem[];
}

export interface SchemaProperty {
  type?: string;
  default?: unknown;
  [key: string]: unknown;
}

export interface Signature {
  properties?: Record<string, SchemaProperty>;
  required?: string[];
  [key: string]: unknown;
}

export interface Handler {
  name: string;
  hooks: string[];
  signature: Signature;
}

export interface Addon {
  name: string;
  script: string;
  script_hash: string;
  handlers: Handler[];
  problem_hooks: string[];
}

export interface Addons {
  version: string | null;
  hash: string | null;
  source: string;
  actions: Addon[];
  problems: LoadProblem[];
}

export interface FunctionsEntry {
  script: string;
  handler: string;
  args: Record<string, unknown>;
  display: string;
  exclude: string;
  order: number;
  valid: boolean;
  problem: { kind: string; message: string } | null;
}

export interface FunctionsFile {
  folder: string;
  file: string;
  digest: string | null;
  error: { kind: string; message: string } | null;
  entries: FunctionsEntry[];
  invalid: { ref: string; kind: string; message: string }[];
}

export interface Functions {
  files: FunctionsFile[];
  problems: LoadProblem[];
}

export interface Upgrade {
  id: string;
  from_tag: string | null;
  from_hash: string;
  to_tag: string;
  to_hash: string;
  schema_before: number;
  schema_after: number;
  tests_run: number | null;
  tests_passed: number | null;
  tests_skipped: number | null;
  snapshot_path: string | null;
  outcome: string;
  started_at: string;
  finished_at: string;
}

export interface Upgrades {
  items: Upgrade[];
  total: number;
}
