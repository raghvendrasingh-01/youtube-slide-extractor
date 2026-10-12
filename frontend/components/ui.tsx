import type { LucideIcon } from "lucide-react";
import {
  Check,
  ChevronDown,
  CircleHelp,
  Loader2,
  Moon,
  Sun,
} from "lucide-react";
import { useEffect, useState } from "react";

export function cn(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

export function BrandMark() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="relative flex h-9 w-9 items-center justify-center rounded-xl bg-forest shadow-md shadow-forest/15">
        <span className="absolute h-4 w-4 rounded-[5px] border-2 border-lime" />
        <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-mint" />
      </div>
      <span className="text-[17px] font-black tracking-[-0.04em] text-ink">
        FrameNotes <span className="font-medium text-muted">AI</span>
      </span>
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "success" | "warning" | "danger" | "dark";
}) {
  const tones = {
    neutral: "border-line bg-panel text-muted",
    success: "border-[#b7e6ca] bg-[#ecfaf1] text-[#267148]",
    warning: "border-[#f1dfad] bg-[#fff9e9] text-[#9b7110]",
    danger: "border-[#f2c9c9] bg-[#fff1f1] text-[#b84d4d]",
    dark: "border-forest/10 bg-forest text-white",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-bold",
        tones[tone],
      )}
    >
      {children}
    </span>
  );
}

export function StepPill({
  index,
  label,
  active,
  done,
}: {
  index: number;
  label: string;
  active?: boolean;
  done?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 text-xs font-bold",
        active ? "text-ink" : "text-muted",
      )}
    >
      <span
        className={cn(
          "flex h-7 w-7 items-center justify-center rounded-full border text-[11px]",
          active
            ? "border-forest bg-forest text-white"
            : done
              ? "border-forest/20 bg-mint text-forest"
              : "border-line bg-panel",
        )}
      >
        {done ? <Check size={14} strokeWidth={3} /> : index}
      </span>
      <span className="hidden sm:inline">{label}</span>
    </div>
  );
}

export function ThemeToggle() {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const stored = window.localStorage.getItem("framenotes-theme");
    const prefers = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const enabled = stored ? stored === "dark" : prefers;
    setDark(enabled);
    document.documentElement.classList.toggle("dark", enabled);
  }, []);
  const toggle = () => {
    const next = !dark;
    setDark(next);
    window.localStorage.setItem("framenotes-theme", next ? "dark" : "light");
    document.documentElement.classList.toggle("dark", next);
  };
  return (
    <button
      type="button"
      aria-label={dark ? "Use light theme" : "Use dark theme"}
      className="btn-ghost !h-9 !w-9 !p-0"
      onClick={toggle}
    >
      {dark ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  );
}

export function SelectControl({
  label,
  icon: Icon,
  value,
  onChange,
  options,
}: {
  label: string;
  icon?: LucideIcon;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string; hint?: string }>;
}) {
  return (
    <label className="relative block">
      <span className="label">
        {Icon ? <Icon size={13} className="mr-1 inline" /> : null}
        {label}
      </span>
      <span className="relative block">
        <select
          className="input-control appearance-none pr-10"
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          {options.map((option) => (
            <option value={option.value} key={option.value}>
              {option.label}
              {option.hint ? ` · ${option.hint}` : ""}
            </option>
          ))}
        </select>
        <ChevronDown
          size={16}
          className="pointer-events-none absolute right-4 top-1/2 -translate-y-1/2 text-forest/50"
        />
      </span>
    </label>
  );
}

export function LoadingLabel({
  children = "Working",
}: {
  children?: React.ReactNode;
}) {
  return (
    <span className="inline-flex items-center gap-2">
      <Loader2 size={16} className="animate-spin" />
      {children}
    </span>
  );
}

export function HelpTip({ text }: { text: string }) {
  return (
    <span title={text} className="inline-flex text-muted">
      <CircleHelp size={13} />
    </span>
  );
}

export function ProgressBar({
  value,
  className,
}: {
  value: number;
  className?: string;
}) {
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(value)}
      className={cn("h-2 overflow-hidden rounded-full bg-forest/10", className)}
    >
      <div
        className="h-full rounded-full bg-forest transition-all duration-500"
        style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
      />
    </div>
  );
}
