import { createContext, useContext } from "react";
import type { ApiConnection } from "./client.ts";

export const ApiContext = createContext<ApiConnection | null>(null);

export function useApi(): ApiConnection {
  const api = useContext(ApiContext);
  if (api === null) throw new Error("useApi must be used inside an ApiContext provider");
  return api;
}
