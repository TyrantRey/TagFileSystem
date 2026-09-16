import { useSearchParams } from "react-router-dom";

import type { FileItem, Page, Tags } from "../api/types";
import { useApi } from "../api/useApi";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  PageHeader,
  Pager,
  PathLink,
  TagChips,
  When,
} from "../components/bits";
import { UploadBox } from "../components/UploadBox";
import { bytes } from "../lib/fmt";

const LIMIT = 50;

/** Every filter lives in the URL (`#/files?tag=a&tag=b&prefix=x&offset=50`),
 * so a view can be linked to and the back button works. */
export function FilesPage() {
  const [search, setSearch] = useSearchParams();
  const tags = search.getAll("tag");
  const name = search.get("name") ?? "";
  const prefix = search.get("prefix") ?? "";
  const deleted = search.get("deleted") === "1";
  const newest = search.get("newest") === "1";
  const offset = Number(search.get("offset") ?? 0) || 0;

  const update = (
    changes: Record<string, string | string[] | null>,
    keepOffset = false,
  ) => {
    const next = new URLSearchParams(search);
    if (!keepOffset) next.delete("offset");
    for (const [key, value] of Object.entries(changes)) {
      next.delete(key);
      if (value === null || value === "") continue;
      if (Array.isArray(value)) value.forEach((v) => next.append(key, v));
      else next.set(key, value);
    }
    setSearch(next);
  };
  const toggleTag = (tag: string) =>
    update({
      tag: tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag],
    });

  const files = useApi<Page<FileItem>>("/files", {
    tag: tags,
    name: name || undefined,
    prefix: prefix || undefined,
    deleted,
    newest,
    limit: LIMIT,
    offset,
  });
  const tagList = useApi<Tags>("/tags", { deleted });

  return (
    <>
      <PageHeader
        title="Files"
        onRefresh={() => {
          files.reload();
          tagList.reload();
        }}
        loading={files.loading}
      />
      <div className="columns">
        <aside className="panel">
          <h2 style={{ marginTop: 0 }}>Tags</h2>
          {tagList.error && <ErrorBox error={tagList.error} />}
          {tagList.data && tagList.data.items.length === 0 && (
            <Empty>No tags yet.</Empty>
          )}
          {tagList.data && (
            <ul className="taglist">
              {tagList.data.items.map((t) => (
                <li key={t.tag_id}>
                  <button
                    type="button"
                    className={tags.includes(t.name) ? "selected" : ""}
                    onClick={() => toggleTag(t.name)}
                  >
                    {t.name}
                  </button>
                  <span className="muted">{t.files}</span>
                </li>
              ))}
            </ul>
          )}
          {tags.length > 0 && (
            <p className="muted">
              Showing files with every selected tag.{" "}
              <button type="button" onClick={() => update({ tag: null })}>
                Clear
              </button>
            </p>
          )}
        </aside>
        <section>
          <form
            className="filters"
            onSubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              update({
                name: String(form.get("name") ?? ""),
                prefix: String(form.get("prefix") ?? ""),
              });
            }}
          >
            <label>
              name{" "}
              <input type="text" name="name" defaultValue={name} key={name} />
            </label>
            <label>
              under{" "}
              <input
                type="text"
                name="prefix"
                defaultValue={prefix}
                key={prefix}
                placeholder="folder"
              />
            </label>
            <button type="submit">Filter</button>
            <label>
              <input
                type="checkbox"
                checked={newest}
                onChange={(event) =>
                  update({ newest: event.target.checked ? "1" : null })
                }
              />
              newest first
            </label>
            <label>
              <input
                type="checkbox"
                checked={deleted}
                onChange={(event) =>
                  update({ deleted: event.target.checked ? "1" : null })
                }
              />
              include deleted
            </label>
          </form>
          <UploadBox
            key={prefix}
            prefix={prefix}
            onUploaded={() => {
              files.reload();
              tagList.reload();
            }}
          />
          {files.error && <ErrorBox error={files.error} />}
          {!files.data && !files.error && <Loading />}
          {files.data && files.data.items.length === 0 && (
            <Empty>No file matches.</Empty>
          )}
          {files.data && files.data.items.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Path</th>
                  <th>Tags</th>
                  <th>Size</th>
                  <th>Type</th>
                  <th>Added</th>
                </tr>
              </thead>
              <tbody>
                {files.data.items.map((f) => (
                  <tr key={f.file_id}>
                    <td>
                      <PathLink path={f.path} />
                      {f.status === "deleted" && (
                        <>
                          {" "}
                          <Badge value="muted" />
                        </>
                      )}
                    </td>
                    <td>
                      <TagChips tags={f.tags} />
                    </td>
                    <td className="num">{bytes(f.size)}</td>
                    <td className="muted">{f.mime_type ?? "–"}</td>
                    <td>
                      <When iso={f.added} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {files.data && (
            <Pager
              total={files.data.total}
              limit={files.data.limit}
              offset={files.data.offset}
              onPage={(next) => update({ offset: String(next) }, true)}
            />
          )}
        </section>
      </div>
    </>
  );
}
