import type { Role } from "../api/types.ts";

const RANK: Record<Role, number> = { viewer: 0, analyst: 1, lead: 2 };

/** True when `role` is at least `required` (`viewer < analyst < lead`). The server is the authority. */
export function roleAtLeast(role: Role | undefined, required: Role): boolean {
  return role !== undefined && RANK[role] >= RANK[required];
}
