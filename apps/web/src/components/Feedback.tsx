import { AlertTriangle, Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { errorMessage } from "../lib/errors.ts";
import { cn } from "../lib/utils.ts";

export function Loading({ label = "Loading", className }: { label?: string; className?: string }) {
  return (
    <p className={cn("flex items-center gap-2 p-4 text-sm text-slate-500", className)} role="status">
      <Loader2 className="size-4 animate-spin" aria-hidden />
      {label}
    </p>
  );
}

export function ErrorBox({ error, title, children }: { error?: unknown; title?: string; children?: ReactNode }) {
  return (
    <div role="alert" className="m-2 flex gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div>
        {title !== undefined && <p className="font-semibold">{title}</p>}
        {error !== undefined && <p>{errorMessage(error)}</p>}
        {children}
      </div>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="p-4 text-sm text-slate-500">{children}</p>;
}
