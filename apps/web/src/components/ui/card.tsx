import type { ComponentProps } from "react";
import { cn } from "../../lib/utils.ts";

/** shadcn/ui Card (copied in, trimmed). */
export function Card({ className, ...props }: ComponentProps<"div">) {
  return <div data-slot="card" className={cn("rounded-lg border border-slate-200 bg-white", className)} {...props} />;
}

export function CardHeader({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex flex-col gap-1 border-b border-slate-100 px-4 py-3", className)} {...props} />;
}

export function CardTitle({ className, ...props }: ComponentProps<"h3">) {
  return <h3 className={cn("text-sm font-semibold text-slate-900", className)} {...props} />;
}

export function CardContent({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("px-4 py-3", className)} {...props} />;
}
