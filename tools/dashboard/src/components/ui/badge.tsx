// shadcn/ui Badge, in the site's mono-label register.
import * as React from "react";
import { cn } from "@/lib/utils";

const variants = {
  default: "border-line-2 text-ink-2",
  live: "border-accent/40 text-accent",
  quiet: "border-line text-ink-3",
} as const;

function Badge({
  className,
  variant = "default",
  ...props
}: React.ComponentProps<"span"> & { variant?: keyof typeof variants }) {
  return (
    <span
      data-slot="badge"
      className={cn(
        "inline-flex items-center gap-1.5 border px-2.5 py-1 font-mono text-[10.5px] font-medium tracking-[0.1em] uppercase",
        variants[variant],
        className,
      )}
      {...props}
    />
  );
}

export { Badge };
