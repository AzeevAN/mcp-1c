import type { QueryClient } from "@tanstack/react-query";

// Сбрасываем данные, а не только stale-флаг: при возврате нельзя на время
// нового fetch показывать карточку удалённого поколения как актуальную.
export async function refreshSourceDependents(client: QueryClient) {
  const filters = { predicate: (query: { queryKey: readonly unknown[] }) =>
    ["card", "roles", "graph", "queries-setup"].includes(String(query.queryKey[0])) };
  await client.cancelQueries(filters);
  await Promise.all([
    client.resetQueries(filters),
    client.invalidateQueries({ queryKey: ["sources"], exact: true }),
    client.invalidateQueries({ queryKey: ["dashboard", "bootstrap"] }),
  ]);
}
