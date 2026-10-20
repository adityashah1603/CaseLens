import type { ComponentProps } from "react";
import { cn } from "../../lib/utils.ts";

const base =
  "w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 placeholder:text-slate-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600 disabled:opacity-50";

/** shadcn/ui Input (copied in, trimmed). */
export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input data-slot="input" className={cn(base, "h-9", className)} {...props} />;
}

export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return <textarea data-slot="textarea" className={cn(base, "min-h-20", className)} {...props} />;
}

export function Select({ className, ...props }: ComponentProps<"select">) {
  return <select data-slot="select" className={cn(base, "h-9", className)} {...props} />;
}

export function Label({ className, ...props }: ComponentProps<"label">) {
  return <label className={cn("text-xs font-medium text-slate-600", className)} {...props} />;
}
