// Shared by the chat page and the admin page: where the login token lives, and two small helpers.
// No business logic: the token only proves who the user is; the backend decides everything else.

import { ABORTED, ApiError } from "./api";

const TOKEN_KEY = "ops.token";

// sessionStorage can throw (private windows, blocked site data): the pages must still work.
export const tokenStore = {
  get(): string | null {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set(value: string): void {
    try {
      sessionStorage.setItem(TOKEN_KEY, value);
    } catch {
      /* the session just will not survive a reload */
    }
  },
  clear(): void {
    try {
      sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* nothing to clear */
    }
  },
};

export const messageOf = (err: unknown, fallback: string): string =>
  err instanceof Error && err.message ? err.message : fallback;

export const isAborted = (err: unknown): boolean =>
  err instanceof ApiError && err.code === ABORTED;
