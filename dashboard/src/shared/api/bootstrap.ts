import { useQuery } from "@tanstack/react-query";

export type DashboardBootstrap = {
  api_version: "v1";
  dashboard_mode: "spa";
  server: {
    status: "ok";
    version: string;
  };
  permissions: {
    read: boolean;
    admin: boolean;
  };
  summary: {
    configurations: number;
    metadata_objects: number;
    code_corpora: number;
    reference_sources: number;
  };
};

async function getBootstrap(): Promise<DashboardBootstrap> {
  const response = await fetch("/api/v1/dashboard/bootstrap", {
    headers: { accept: "application/json" },
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new Error(`API дашборда ответил ${response.status}.`);
  }
  return response.json() as Promise<DashboardBootstrap>;
}

export function useBootstrap() {
  return useQuery({
    queryKey: ["dashboard", "bootstrap"],
    queryFn: getBootstrap,
  });
}
