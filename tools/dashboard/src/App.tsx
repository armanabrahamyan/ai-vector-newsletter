import { useCallback, useEffect, useRef, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  busiestHour,
  mergePages,
  fetchSnapshot,
  formatDay,
  RateLimited,
  lastNDays,
  pageLabel,
  refLabel,
  sizeLabel,
  todayCount,
  todayHourly,
  weekCount,
  type NamedStat,
  type Snapshot,
} from "@/gc";
import { cn } from "@/lib/utils";

const LAUNCH = "2026-08-14";

const RANGES = [
  { key: LAUNCH, label: "Since launch" },
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
] as const;

function rangeStart(key: string): string {
  if (key === "7d" || key === "30d") {
    const d = new Date();
    d.setDate(d.getDate() - (key === "7d" ? 6 : 29));
    return d.toISOString().slice(0, 10);
  }
  return key;
}

export default function App() {
  const [range, setRange] = useState<string>(LAUNCH);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const inFlight = useRef(false);
  const load = useCallback(async (r: string) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const s = await fetchSnapshot(rangeStart(r));
      setSnap(s);
      setError(null);
    } catch (e) {
      setError(
        e instanceof RateLimited
          ? `rate-limited — the API budget resets in ${Math.ceil(e.retryAfterSec / 60)}m; press refresh then`
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }, []);

  // No polling: one fetch on load and on range change, then only when the
  // operator presses refresh. Nine API calls per press, zero in between.
  useEffect(() => {
    load(range);
  }, [range, load]);

  return (
    <main className="mx-auto min-h-screen max-w-[980px] bg-paper px-6 pt-12 pb-16 shadow-[0_1px_3px_rgba(12,12,12,.04),0_12px_60px_rgba(12,12,12,.06)] sm:px-14">
      <Masthead busy={busy} updatedAt={snap?.fetchedAt ?? null} error={error} onRefresh={() => load(range)} />

      <div className="mt-8 flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-[30px] font-medium tracking-[-0.015em]">Readership</h1>
        <div className="flex gap-1.5">
          {RANGES.map((r) => (
            <button
              key={r.key}
              onClick={() => setRange(r.key)}
              className={cn(
                "cursor-pointer border px-3 py-1 font-mono text-[11px] tracking-[0.08em] uppercase transition-colors",
                range === r.key
                  ? "border-ink bg-ink text-paper"
                  : "border-line-2 text-ink-2 hover:border-ink-2",
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>
      <p className="mt-1 max-w-[60ch] text-[15.5px] text-ink-2">
        Visits to aivector.day, counted by GoatCounter — no cookies, no personal
        data. Counting began 14 August 2026.
      </p>

      {snap ? <Body snap={snap} /> : <Loading error={error} />}

      <footer className="mt-14 flex flex-wrap justify-between gap-3 border-t-2 border-ink pt-3 font-mono text-[11px] text-ink-3">
        <span>
          AI<span className="text-accent">/</span>Vector · aivector.day
        </span>
        <span>
          local dashboard · token stays on this machine · refreshes on demand
        </span>
      </footer>
    </main>
  );
}

function Masthead({
  busy,
  updatedAt,
  error,
  onRefresh,
}: {
  busy: boolean;
  updatedAt: Date | null;
  error: string | null;
  onRefresh: () => void;
}) {
  return (
    <header className="border-b-2 border-ink pb-3">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div className="text-[26px] font-medium tracking-[-0.02em]">
          <span className="mr-3 inline-block h-[19px] w-[19px] translate-y-[1px] text-ink">
            <svg viewBox="0 0 100 100" role="presentation" className="block h-full w-full">
              <path
                fillRule="evenodd"
                fill="currentColor"
                d="M0 0L100 0L100 100L0 100ZM18.37 0L36.37 84.7L100 16.46L100 0L93.47 0L45.63 51.3L34.72 0Z"
              />
            </svg>
          </span>
          AI<span className="text-accent">/</span>Vector
        </div>
        <div className="flex items-baseline gap-3">
          {error && (
            <Badge variant="quiet">
              {error.startsWith("rate-limited") ? error : "api unreachable"}
            </Badge>
          )}
          <button
            onClick={onRefresh}
            disabled={busy}
            title="Fetch fresh numbers"
            className="cursor-pointer border border-ink bg-ink px-3.5 py-1.5 font-mono text-[11px] font-medium tracking-[0.1em] text-paper uppercase transition-colors hover:bg-accent hover:border-accent disabled:cursor-default disabled:border-ink-3 disabled:bg-ink-3"
          >
            {busy ? "↻ Refreshing…" : "↻ Refresh"}
          </button>
          {!error && updatedAt && !busy && (
            <span className="font-mono text-[10.5px] text-ink-3">
              updated {updatedAt.toLocaleTimeString("en-AU", { hour: "numeric", minute: "2-digit" })}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}

function Loading({ error }: { error: string | null }) {
  const text = !error
    ? "Reading the counter…"
    : error.startsWith("rate-limited")
      ? `The GoatCounter API is ${error}.`
      : `Cannot reach the GoatCounter API (${error}). Is GOATCOUNTER_API_TOKEN set in the repo .env?`;
  return (
    <p className="mt-16 text-center font-mono text-[13px] text-ink-3">{text}</p>
  );
}

function Body({ snap }: { snap: Snapshot }) {
  const days = lastNDays(snap.total, 30);
  const hourly = todayHourly(snap.total);
  const peak = busiestHour(snap.total);
  const pages = mergePages(snap.pages);
  const pagesTotal = pages.reduce((a, p) => a + p.count, 0);

  return (
    <>
      {/* ---- headline numbers ---- */}
      <div className="mt-9 grid grid-cols-1 border-t-2 border-ink sm:grid-cols-4">
        <Kpi label="Today" value={todayCount(snap.total)} detail="pageviews" />
        <Kpi label="Last 7 days" value={weekCount(snap.total)} detail="pageviews" />
        <Kpi label="Range total" value={snap.total.total} detail={`since ${formatDay(snap.start)}`} />
        <Kpi
          label="Busiest hour today"
          value={peak.count ? `${peak.hour}:00` : "—"}
          detail={peak.count ? `${peak.count} view${peak.count === 1 ? "" : "s"}` : "no visits yet today"}
          last
        />
      </div>

      {/* ---- daily trend ---- */}
      <Card className="mt-10">
        <CardHeader>
          <CardTitle>Day by day</CardTitle>
          <CardDescription>
            Daily pageviews across the range. The dot is today.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DailyChart days={days} />
        </CardContent>
      </Card>

      {/* ---- today hourly ---- */}
      <Card className="mt-9">
        <CardHeader>
          <CardTitle>Today, hour by hour</CardTitle>
          <CardDescription>Vermilion marks hours with at least one visit.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex h-[110px] items-end gap-[3px] border-b border-line-2">
            {hourly.map((v, h) => (
              <div
                key={h}
                title={`${String(h).padStart(2, "0")}:00 — ${v} view${v === 1 ? "" : "s"}`}
                className={cn("min-w-0 flex-1 rounded-t-[1px]", v ? "bg-accent" : "bg-line")}
                style={{ height: `${Math.max((v / Math.max(...hourly, 1)) * 100, 3)}%` }}
              />
            ))}
          </div>
          <div className="mt-1.5 flex justify-between font-mono text-[10.5px] text-ink-3">
            <span>midnight</span>
            <span>6 am</span>
            <span>noon</span>
            <span>6 pm</span>
            <span>11 pm</span>
          </div>
        </CardContent>
      </Card>

      {/* ---- pages ---- */}
      <Card className="mt-9">
        <CardHeader>
          <CardTitle>What was read</CardTitle>
          <CardDescription>
            Every page, by pageviews. Issues are labelled by number from the archive.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {pages.length === 0 ? (
            <Empty>No pageviews in this range yet.</Empty>
          ) : (
            <table className="w-full border-collapse">
              <tbody>
                {pages.map((p) => {
                  const { name, detail } = pageLabel(p);
                  return (
                    <tr key={p.path} className="border-b border-line last:border-b-0">
                      <td className="w-[52%] py-2.5 pr-2 align-baseline">
                        <span className="block text-[15.5px]">{name}</span>
                        <span className="block font-mono text-[10.5px] text-ink-3">{detail}</span>
                      </td>
                      <td className="w-[8%] py-2.5 text-right align-baseline font-mono text-[13px] tabular-nums">
                        {p.count}
                      </td>
                      <td className="w-[40%] py-2.5 pl-4 align-middle">
                        <span
                          className="block h-[10px] min-w-[2px] rounded-[1px] bg-accent"
                          style={{ width: `${(p.count / Math.max(pagesTotal, 1)) * 100}%` }}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {/* ---- audience ---- */}
      <div className="mt-9 grid grid-cols-1 gap-x-8 gap-y-9 sm:grid-cols-3">
        <MiniList title="Referrers" items={snap.referrers} labeler={refLabel} />
        <MiniList title="Countries" items={snap.locations} />
        <MiniList title="Languages" items={snap.languages} />
        <MiniList title="Browsers" items={snap.browsers} />
        <MiniList title="Systems" items={snap.systems} />
        <MiniList
          title="Screen sizes"
          items={snap.sizes.filter((s) => s.count > 0)}
          labeler={sizeLabel}
        />
      </div>

      {/* ---- campaigns, only when they exist ---- */}
      {snap.campaigns.length > 0 && (
        <div className="mt-9">
          <MiniList title="Campaigns" items={snap.campaigns} />
        </div>
      )}
    </>
  );
}

function Kpi({
  label,
  value,
  detail,
  last,
}: {
  label: string;
  value: number | string;
  detail: string;
  last?: boolean;
}) {
  return (
    <div className={cn("border-line px-5 py-4 sm:border-r", last && "sm:border-r-0")}>
      <span className="kicker">{label}</span>
      <div className="mt-1.5 text-[42px] leading-[1.1] font-normal tracking-[-0.02em] tabular-nums">
        {value}
      </div>
      <div className="mt-0.5 font-mono text-[11px] text-ink-3">{detail}</div>
    </div>
  );
}

function DailyChart({ days }: { days: { day: string; daily: number }[] }) {
  const W = 880;
  const H = 170;
  if (days.length < 2) {
    return <Empty>The trend appears once there is more than one day of data.</Empty>;
  }
  const max = Math.max(...days.map((d) => d.daily), 1);
  const pts = days.map((d, i) => ({
    x: (i / (days.length - 1)) * W,
    y: H - 6 - (d.daily / max) * (H - 28),
  }));
  const line = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const area = `0,${H} ${line} ${W},${H}`;
  const end = pts[pts.length - 1];
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-[170px] w-full">
        <polygon points={area} fill="#9f3b2e18" />
        <polyline points={line} fill="none" stroke="#9f3b2e" strokeWidth={2} />
        <circle cx={end.x} cy={end.y} r={4} fill="#9f3b2e" />
      </svg>
      <div className="mt-1.5 flex justify-between font-mono text-[10.5px] text-ink-3">
        <span>{formatDay(days[0].day)}</span>
        <span>{formatDay(days[days.length - 1].day)}</span>
      </div>
    </div>
  );
}

function MiniList({
  title,
  items,
  labeler = (s: NamedStat) => s.name || s.id || "Unknown",
}: {
  title: string;
  items: NamedStat[];
  labeler?: (s: NamedStat) => string;
}) {
  return (
    <div>
      <h3 className="kicker border-t border-line-2 pt-2.5">{title}</h3>
      {items.length === 0 ? (
        <Empty>Nothing yet.</Empty>
      ) : (
        <ul className="mt-2">
          {items.slice(0, 8).map((it, i) => (
            <li
              key={`${it.id ?? it.name}-${i}`}
              className="flex justify-between gap-3 border-b border-line py-1.5 text-[15px] last:border-b-0"
            >
              <span className="truncate">{labeler(it)}</span>
              <span className="font-mono text-[12.5px] text-ink-2 tabular-nums">{it.count}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="mt-3 text-[14.5px] text-ink-3 italic">{children}</p>;
}
