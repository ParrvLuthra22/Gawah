import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";
import { twMerge } from "tailwind-merge";

const variants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg px-3.5 py-2 text-sm font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        primary: "bg-[#0069d3] text-white hover:bg-[#0057b2]",
        outline: "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50",
      },
    },
    defaultVariants: { variant: "primary" },
  },
);

export function Button({
  className, variant, asChild = false, type, ...props
}: ComponentProps<"button"> & VariantProps<typeof variants> & { asChild?: boolean }) {
  const Element = asChild ? Slot : "button";
  return <Element className={twMerge(variants({ variant }), className)}
    type={asChild ? undefined : type ?? "button"} {...props} />;
}
