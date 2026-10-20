import { cva } from "class-variance-authority";

export const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600 focus-visible:ring-offset-1 disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-slate-900 text-white hover:bg-slate-800",
        secondary: "border border-slate-300 bg-white text-slate-900 hover:bg-slate-50",
        ghost: "text-slate-700 hover:bg-slate-100",
        destructive: "bg-red-700 text-white hover:bg-red-800",
        link: "text-sky-700 underline-offset-2 hover:underline",
      },
      size: {
        default: "h-9 px-3.5 py-2",
        sm: "h-7 px-2.5 text-xs",
        lg: "h-10 px-5",
        icon: "size-8",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);
