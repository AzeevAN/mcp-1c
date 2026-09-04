import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type PropsWithChildren, useState } from "react";
import { useBootstrap } from "../shared/api/bootstrap";
import { useAdminSources } from "../shared/api/sourceAdmin";

function UploadObserver() {
  const bootstrap = useBootstrap();
  // Наблюдение продолжается при переходе с «Источников» на карточку.
  useAdminSources(Boolean(bootstrap.data?.permissions.admin));
  return null;
}

export function AppProviders({ children }: PropsWithChildren) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: false,
            retry: 1,
            staleTime: 10_000,
          },
        },
      }),
  );

  return <QueryClientProvider client={queryClient}><UploadObserver />{children}</QueryClientProvider>;
}
