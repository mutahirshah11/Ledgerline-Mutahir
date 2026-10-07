import { cn } from "@/lib/utils";

// contracts/components.md "Banner"; red/amber tones, role="alert" for error (error is the only
// tone this app actually shows red, per ui-copy.md §4 "Error banner"; warning covers the amber
// "Summary not delivered" case). Text is always present (never colour-only, NFR-09).
export type BannerProps = {
  tone: "error" | "warning" | "info";
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
};

const TONE_CLASS: Record<BannerProps["tone"], string> = {
  error: "bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-300",
  warning: "bg-amber-100 text-amber-900 dark:bg-amber-900/30 dark:text-amber-300",
  info: "bg-blue-100 text-blue-900 dark:bg-blue-900/30 dark:text-blue-300",
};

export function Banner({ tone, title, children, action }: BannerProps) {
  return (
    <div
      role={tone === "error" ? "alert" : undefined}
      className={cn("rounded-md p-3", TONE_CLASS[tone])}
    >
      <p className="font-medium">{title}</p>
      {children}
      {action}
    </div>
  );
}
