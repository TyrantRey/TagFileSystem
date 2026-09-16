// Put files into the root from the browser (DESIGN/v0-5-0.md §12.3): one
// `POST /files/upload` per file, the bytes as the body, into the folder
// being browsed unless another is named. Each answer is one line: what was
// written and which handlers apply to it.

import { useState } from "react";

import { ApiError, apiUpload } from "../api/client";
import type { UploadResult } from "../api/types";

interface Outcome {
  name: string;
  path: string;
  status: "ok" | "err";
  text: string;
}

export function UploadBox({
  prefix,
  onUploaded,
}: {
  prefix: string;
  onUploaded: () => void;
}) {
  const [folder, setFolder] = useState(prefix);
  const [overwrite, setOverwrite] = useState(false);
  const [busy, setBusy] = useState(false);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const [files, setFiles] = useState<File[]>([]);
  const [inputKey, setInputKey] = useState(0);

  const target = (name: string): string => {
    const dir = folder.trim().replace(/^\/+|\/+$/g, "");
    return dir ? `${dir}/${name}` : name;
  };

  const upload = async () => {
    setBusy(true);
    const results: Outcome[] = [];
    for (const file of files) {
      const path = target(file.name);
      try {
        const result = await apiUpload<UploadResult>(
          "/files/upload",
          { path, overwrite },
          file,
        );
        const applies = result.applies.length
          ? `; runs ${result.applies.join(", ")}`
          : "";
        results.push({
          name: file.name,
          path: result.path,
          status: "ok",
          text: `${result.created ? "created" : "replaced"}${applies}`,
        });
      } catch (failure) {
        results.push({
          name: file.name,
          path,
          status: "err",
          text: failure instanceof ApiError ? failure.message : String(failure),
        });
      }
      setOutcomes([...results]);
    }
    setBusy(false);
    setFiles([]);
    setInputKey((n) => n + 1); // a fresh input: the same file can be chosen again
    if (results.some((r) => r.status === "ok")) onUploaded();
  };

  return (
    <details className="upload">
      <summary>Upload</summary>
      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          if (files.length) void upload();
        }}
      >
        <input
          key={inputKey}
          type="file"
          multiple
          aria-label="files to upload"
          disabled={busy}
          onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
        />
        <label>
          into{" "}
          <input
            type="text"
            value={folder}
            placeholder="folder (root)"
            aria-label="destination folder"
            disabled={busy}
            onChange={(event) => setFolder(event.target.value)}
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={overwrite}
            disabled={busy}
            onChange={(event) => setOverwrite(event.target.checked)}
          />
          overwrite
        </label>
        <button type="submit" disabled={busy || files.length === 0}>
          {busy
            ? "Uploading…"
            : `Upload${files.length ? ` ${files.length}` : ""}`}
        </button>
      </form>
      {outcomes.length > 0 && (
        <ul className="upload-outcomes">
          {outcomes.map((o) => (
            <li key={o.path} className={o.status === "err" ? "err" : ""}>
              <span className="mono">{o.path}</span>: {o.text}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
