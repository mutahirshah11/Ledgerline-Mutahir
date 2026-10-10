"use client";

// 006-web T033: FileDropZone (contracts/components.md "Screen-local components"; SCR-03-AC2,
// AC3; FR-008, FR-009; NFR-09). A labelled group that takes files by drop or by a keyboard
// reachable "Choose files" button, lists them with their problem inline, and (register pages)
// numbers them in list order with "Move up" / "Move down" buttons. The number is a label only.

import { AlertCircle, ArrowDown, ArrowUp, UploadCloud, X } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";

import { Button } from "@/components/ui/button";
import { fileProblemText, type FileProblem } from "@/lib/file-rules";
import { cn } from "@/lib/utils";

export type FileDropZoneProps = {
  label: string;
  /** Form name of the file input ("register", "erp", "sheet"). */
  name: string;
  accept: string;
  multiple: boolean;
  files: File[];
  /** One entry per file (list order). */
  problems: (FileProblem | null)[];
  /** A problem of the zone as a whole (none, too many…). */
  zoneProblem?: FileProblem | null;
  onFiles: (files: File[]) => void;
  onRemove: (index: number) => void;
  /** Register pages: move the file at `from` to `to`. Shows numbering and Move buttons. */
  onReorder?: (from: number, to: number) => void;
  disabled?: boolean;
};

export function FileDropZone({
  label,
  name,
  accept,
  multiple,
  files,
  problems,
  zoneProblem,
  onFiles,
  onRemove,
  onReorder,
  disabled,
}: FileDropZoneProps) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const numbered = !!onReorder;

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    if (disabled) return;
    const dropped = Array.from(e.dataTransfer.files);
    if (dropped.length) onFiles(dropped);
  };

  return (
    <fieldset
      className={cn(
        "min-w-0 rounded-xl border-2 border-dashed p-4 transition-colors",
        over
          ? "border-emerald-400 bg-emerald-50/70 dark:border-emerald-600 dark:bg-emerald-950/20"
          : "border-border bg-muted/10",
      )}
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      disabled={disabled}
    >
      <legend className="flex items-center gap-1.5 px-1 text-sm font-semibold text-foreground">
        <UploadCloud className="h-4 w-4 text-emerald-600" aria-hidden="true" />
        {label}
      </legend>
      <p className="mt-1 text-sm text-muted-foreground">
        Drop {multiple ? "files" : "a file"} here, or
      </p>
      <input
        ref={input}
        type="file"
        name={name}
        accept={accept}
        multiple={multiple}
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(e) => {
          const picked = Array.from(e.target.files ?? []);
          e.target.value = "";
          if (picked.length) onFiles(picked);
        }}
      />
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="mt-2"
        disabled={disabled}
        onClick={() => input.current?.click()}
        aria-label={`Choose ${multiple ? "files" : "file"} for ${label}`}
      >
        Choose {multiple ? "files" : "file"}
      </Button>

      {files.length > 0 ? (
        <ol
          aria-label={numbered ? `${label} in order` : `${label} files`}
          className="mt-3 flex flex-col gap-1.5"
        >
          {files.map((f, i) => {
            const problem = problems[i];
            return (
              <li
                key={`${f.name}-${i}`}
                className={cn(
                  "flex flex-wrap items-center gap-2 rounded-lg border bg-background px-2.5 py-1.5 text-sm",
                  problem ? "border-red-200 dark:border-red-900" : "border-border",
                )}
              >
                {numbered ? (
                  <span className="flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full bg-emerald-100 px-1 text-xs font-semibold text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300">
                    {i + 1}
                  </span>
                ) : null}
                {numbered ? <span className="font-medium">Page {i + 1}</span> : null}
                <span className="min-w-0 break-all">{f.name}</span>
                {problem ? (
                  <span className="inline-flex items-center gap-1 text-red-700 dark:text-red-400">
                    <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    {fileProblemText(problem)}
                  </span>
                ) : null}
                <span className="mt-1 flex w-full basis-full flex-wrap justify-end gap-1">
                  {numbered ? (
                    <>
                      <Button
                        type="button"
                        variant="ghost"
                        size="xs"
                        disabled={disabled || i === 0}
                        onClick={() => onReorder!(i, i - 1)}
                      >
                        <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
                        Move up<span className="sr-only"> page {i + 1}</span>
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="xs"
                        disabled={disabled || i === files.length - 1}
                        onClick={() => onReorder!(i, i + 1)}
                      >
                        <ArrowDown className="h-3.5 w-3.5" aria-hidden="true" />
                        Move down<span className="sr-only"> page {i + 1}</span>
                      </Button>
                    </>
                  ) : null}
                  <Button type="button" variant="ghost" size="xs" disabled={disabled} onClick={() => onRemove(i)}>
                    <X className="h-3.5 w-3.5" aria-hidden="true" />
                    Remove<span className="sr-only"> {f.name}</span>
                  </Button>
                </span>
              </li>
            );
          })}
        </ol>
      ) : null}
      {zoneProblem ? (
        <p className="mt-2 flex items-center gap-1.5 text-sm text-red-700 dark:text-red-400">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          {fileProblemText(zoneProblem)}
        </p>
      ) : null}
    </fieldset>
  );
}
