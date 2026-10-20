import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";
import { cn } from "../../lib/utils.ts";

const badgeVariants = cva("inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium", {
  variants: {
    tone: {
      neutral: "bg-slate-100 text-slate-700",
      info: "bg-sky-50 text-sky-800 ring-1 ring-sky-200",
      success: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
      warning: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
      danger: "bg-red-50 text-red-800 ring-1 ring-red-200",
    },
  },
  defaultVariants: { tone: "neutral" },
});

/** shadcn/ui Badge (copied in, tones adapted). */
export function Badge({ className, tone, ...props }: ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ tone }), className)} {...props} />;
}
