import { useCallback, useEffect, useState } from "react";

import { api, ApiError, type Params } from "./client";

export interface ApiState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

/** One endpoint as React state: fetched on mount and whenever `path` or
 * `params` change (by value), refetched by `reload()`; the last good `data`
 * stays visible while a reload is in flight. No polling — every page has a
 * Refresh button instead. */
export function useApi<T>(path: string, params?: Params): ApiState<T> {
  // Callers build `params` afresh each render: compare by value.
  const key = JSON.stringify([path, params ?? {}]);
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<Omit<ApiState<T>, "reload">>({
    data: null,
    error: null,
    loading: true,
  });
  const reload = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    const [wantedPath, wantedParams] = JSON.parse(key) as [string, Params];
    const controller = new AbortController();
    setState((previous) => ({ ...previous, loading: true }));
    api<T>(wantedPath, wantedParams, { signal: controller.signal })
      .then((data) => setState({ data, error: null, loading: false }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const failure =
          error instanceof ApiError ? error : new ApiError(0, String(error));
        setState((previous) => ({
          data: previous.data,
          error: failure,
          loading: false,
        }));
      });
    return () => controller.abort();
  }, [key, tick]);

  return { ...state, reload };
}
