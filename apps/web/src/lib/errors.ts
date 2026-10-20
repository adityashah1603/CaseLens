import { ApiError } from "../api/client.ts";

/** A user-facing message for an API or runtime error. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "Your role on this case does not allow this action.";
    if (error.status === 404) return "Not found, or you are not a member of this case.";
    return error.message;
  }
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}
