// A file's tags as editable chips (DESIGN/v0-5-0.md §12.3): an × on every
// tag the name does not spell, an input to add one. Each change is one
// `POST /file/tags`; the owner reloads what it shows afterwards.

import { useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, apiPost } from "../api/client";
import type { TagsResult } from "../api/types";

export function TagEditor({
  path,
  tags,
  nameTags,
  onChanged,
}: {
  path: string;
  tags: string[];
  nameTags: string[];
  onChanged: (result: TagsResult) => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const fixed = new Set(nameTags);

  const change = async (body: { add?: string[]; remove?: string[] }) => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const result = await apiPost<TagsResult>("/file/tags", { path }, body);
      const parts: string[] = [];
      if (result.added.length) parts.push(`added ${result.added.join(", ")}`);
      if (result.removed.length)
        parts.push(`removed ${result.removed.join(", ")}`);
      if (result.kept.length)
        parts.push(`${result.kept.join(", ")}: spelled by the name, kept`);
      if (result.applies.length)
        parts.push(`applies: ${result.applies.join(", ")}`);
      setNote(parts.join("; ") || "nothing changed");
      setDraft("");
      onChanged(result);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="tag-editor">
      <span className="chips">
        {tags.length === 0 && <span className="muted">no tags</span>}
        {tags.map((tag) => (
          <span key={tag} className="chip">
            <Link to={`/files?tag=${encodeURIComponent(tag)}`}>{tag}</Link>
            {fixed.has(tag) ? (
              <span className="muted" title="spelled by the file's name">
                {" "}
                ·
              </span>
            ) : (
              <button
                type="button"
                className="chip-remove"
                aria-label={`remove tag ${tag}`}
                title="remove"
                disabled={busy}
                onClick={() => void change({ remove: [tag] })}
              >
                ×
              </button>
            )}
          </span>
        ))}
      </span>
      <form
        className="tag-add"
        onSubmit={(event) => {
          event.preventDefault();
          const names = draft
            .split(/[\s,]+/)
            .map((n) => n.trim())
            .filter(Boolean);
          if (names.length) void change({ add: names });
        }}
      >
        <input
          type="text"
          value={draft}
          placeholder="add a tag"
          aria-label="tag to add"
          disabled={busy}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button type="submit" disabled={busy || !draft.trim()}>
          Add
        </button>
      </form>
      {error && (
        <div className="banner banner-err" role="alert">
          {error}
        </div>
      )}
      {note && <p className="muted">{note}</p>}
    </div>
  );
}
