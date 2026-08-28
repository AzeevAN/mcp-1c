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
  authentication: {
    read_required: boolean;
    admin_available: boolean;
    session_level: "read" | "admin" | null;
  };
  summary: {
    configurations: number;
    metadata_objects: number;
    code_corpora: number;
    reference_sources: number;
  };
};

export class DashboardApiError extends Error {
  constructor(public readonly status: number) {
    super(`API дашборда ответил ${status}.`);
  }
}

async function getBootstrap(): Promise<DashboardBootstrap> {
  const response = await fetch("/api/v1/dashboard/bootstrap", {
    headers: { accept: "application/json" },
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new DashboardApiError(response.status);
  }
  return response.json() as Promise<DashboardBootstrap>;
}

export function useBootstrap() {
  return useQuery({
    queryKey: ["dashboard", "bootstrap"],
    queryFn: getBootstrap,
  });
}
