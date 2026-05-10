import { AnimatePresence, motion } from "framer-motion";
import { Activity, Bitcoin, Globe2, Landmark, Layers, Menu, Settings2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { NavLink, useLocation, useOutlet } from "react-router-dom";
import {
  getHealth,
  getUsFx,
} from "../api";
import usePolling from "../hooks/usePolling";
import { usePortfolio } from "../context/PortfolioContext";
import { krw, pct, relativeTime, usd } from "../lib/format";
import { pageVariants } from "./ui/motion";

const NAV = [
  { to: "/",       label: "BTC",        sub: "비트코인",   accent: "var(--accent-btc)",    icon: Bitcoin  },
  { to: "/kr",     label: "국내 주식",   sub: "KR Market",  accent: "var(--accent-kr)",     icon: Landmark },
  { to: "/us",     label: "미국 주식",   sub: "US Market",  accent: "var(--accent-us)",     icon: Globe2   },
  { to: "/pro",    label: "Pro",        sub: "도킹 워크스페이스", accent: "var(--accent-btc)", icon: Layers   },
];

function metricTone(v) {
  return Number(v || 0) >= 0 ? "text-[color:var(--color-profit)]" : "text-[color:var(--color-loss)]";
}

function PortfolioRow({ label, value, delta, pnlKrw, accent }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-xl px-2.5 py-2 hover:bg-white/[0.03] transition-colors">
      <div className="flex items-center gap-2 min-w-0">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: accent }} />
        <span className="text-xs text-[color:var(--text-secondary)] truncate">{label}</span>
      </div>
      <div className="text-right shrink-0">
        <div className="numeric text-xs font-semibold text-[color:var(--text-primary)]">{value}</div>
        {pnlKrw != null && pnlKrw !== 0 ? (
          <div className={`numeric text-[10px] ${metricTone(pnlKrw)}`}>
            {Number(pnlKrw) >= 0 ? "+" : ""}{krw(pnlKrw)}
          </div>
        ) : delta != null ? (
          <div className={`numeric text-[10px] ${metricTone(delta)}`}>{pct(delta)}</div>
        ) : null}
      </div>
    </div>
  );
}

function HealthDot({ health }) {
  const ok = health?.status === "ok";
  return (
    <span
      className="inline-block h-2 w-2 rounded-full"
      style={{
        background: ok ? "var(--color-profit)" : "var(--color-loss)",
        boxShadow: ok ? "0 0 6px var(--color-profit)" : "0 0 6px var(--color-loss)",
      }}
    />
  );
}

function Sidebar({ open, onClose, summary, health, updatedAt, now }) {
  const location = useLocation();

  return (
    <>
      {/* 모바일 오버레이 */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-black/60 backdrop-blur-sm lg:hidden"
          onClick={onClose}
        />
      )}

      <aside
        className={`
          fixed inset-y-0 left-0 z-40 flex w-[220px] flex-col
          border-r border-[color:var(--border-subtle)]
          bg-[rgba(10,10,15,0.97)] backdrop-blur-xl
          transition-transform duration-300 ease-in-out
          ${open ? "translate-x-0" : "-translate-x-full"}
          lg:translate-x-0 lg:z-20
        `}
      >
        {/* 로고 */}
        <div className="flex h-14 items-center justify-between gap-3 border-b border-[color:var(--border-subtle)] px-4">
          <div className="flex items-center gap-2.5">
            <div className="grid h-8 w-8 shrink-0 place-items-center rounded-xl border border-white/10 bg-[linear-gradient(135deg,rgba(247,147,26,0.3),rgba(59,130,246,0.25))]">
              <Activity size={15} />
            </div>
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[color:var(--text-muted)]">OpenClaw</div>
              <div className="text-xs font-semibold text-[color:var(--text-primary)]">트레이딩 터미널</div>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="lg:hidden grid h-7 w-7 place-items-center rounded-lg text-[color:var(--text-muted)] hover:text-white hover:bg-white/[0.06]"
          >
            <X size={14} />
          </button>
        </div>

        {/* 내비게이션 */}
        <div className="px-3 pt-4">
          <div className="mb-2 px-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-[color:var(--text-muted)]">
            내비게이션
          </div>
          <nav className="space-y-0.5">
            {NAV.map(({ to, label, sub, accent, icon: Icon }) => (
              <NavLink key={to} to={to} end={to === "/"} onClick={onClose}>
                {({ isActive }) => (
                  <div
                    className={`relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition-all ${
                      isActive
                        ? "bg-white/[0.07] text-white"
                        : "text-[color:var(--text-secondary)] hover:bg-white/[0.04] hover:text-white"
                    }`}
                  >
                    {isActive && (
                      <motion.span
                        layoutId="sidebar-active"
                        className="absolute inset-0 rounded-xl"
                        style={{ boxShadow: `inset 1px 0 0 ${accent}`, background: `${accent}08` }}
                        transition={{ type: "spring", stiffness: 400, damping: 30 }}
                      />
                    )}
                    <span
                      className="relative z-10 grid h-7 w-7 shrink-0 place-items-center rounded-lg border border-white/[0.07]"
                      style={isActive ? { background: `${accent}22`, borderColor: `${accent}44` } : {}}
                    >
                      <Icon size={14} style={isActive ? { color: accent } : {}} />
                    </span>
                    <div className="relative z-10 min-w-0">
                      <div className="font-medium leading-tight truncate">{label}</div>
                      <div className="text-[10px] text-[color:var(--text-muted)] leading-tight truncate">{sub}</div>
                    </div>
                    {isActive && (
                      <span
                        className="relative z-10 ml-auto h-1.5 w-1.5 shrink-0 rounded-full"
                        style={{ background: accent, boxShadow: `0 0 8px ${accent}` }}
                      />
                    )}
                  </div>
                )}
              </NavLink>
            ))}
          </nav>
        </div>

        {/* 포트폴리오 현황 */}
        <div className="mt-4 border-t border-[color:var(--border-subtle)] px-3 pt-4">
          <div className="mb-1.5 px-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-[color:var(--text-muted)]">
            포트폴리오
          </div>
          {summary.map((item) => (
            <PortfolioRow key={item.label} {...item} />
          ))}
        </div>

        {/* 하단: 시스템 상태 */}
        <div className="mt-auto border-t border-[color:var(--border-subtle)] px-4 py-3 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <HealthDot health={health} />
              <span className="text-xs text-[color:var(--text-secondary)]">
                시스템 {health?.status === "ok" ? "정상" : health?.status === "degraded" ? "주의" : "확인 중"}
              </span>
            </div>
            <span className="numeric text-[10px] text-[color:var(--text-muted)]">
              {new Intl.DateTimeFormat("ko-KR", { hour: "2-digit", minute: "2-digit" }).format(now)}
            </span>
          </div>
          <div className="text-[10px] text-[color:var(--text-muted)]">
            갱신: <span className="numeric">{relativeTime(updatedAt, now)}</span>
          </div>
        </div>
      </aside>
    </>
  );
}

export default function Layout() {
  const location = useLocation();
  const outlet = useOutlet();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [now, setNow] = useState(Date.now());

  const { btcPortfolio: btc, krPortfolio: kr, usPortfolio: us } = usePortfolio();
  const { data: fx } = usePolling(getUsFx, 60000);
  const { data: health, updatedAt } = usePolling(getHealth, 30000);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(id);
  }, []);

  // 페이지 이동 시 사이드바 자동 닫기 (모바일)
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  const summary = useMemo(() => {
    const usdKrw = Number(fx?.rate || fx?.usdkrw || 1350);

    const btcAsset = Number(btc?.summary?.estimated_asset || 0);
    const btcPnl = Number(btc?.summary?.unrealized_pnl || 0);
    const btcPnlPct = Number(btc?.summary?.unrealized_pnl_pct || 0);

    const krDeposit = Number(kr?.deposit || 0);
    const krEval = Number(kr?.total_evaluation || 0);
    const krPurchase = Number(kr?.total_purchase || 0);
    const krAsset = krDeposit + krEval;
    const krPnl = krEval - krPurchase;
    const krPnlPct = krPurchase > 0 ? (krPnl / krPurchase) * 100 : 0;

    const usCurrentUsd = Number(us?.summary?.total_current || 0);
    const usInvestedUsd = Number(us?.summary?.total_invested || 0);
    const usPnlUsd = Number(us?.summary?.total_pnl_usd || (usCurrentUsd - usInvestedUsd) || 0);
    const usPnlKrw = usPnlUsd * usdKrw;
    const usAssetKrw = usCurrentUsd * usdKrw;
    const usPnlPct = Number(us?.summary?.total_pnl_pct || 0);

    const totalAsset = btcAsset + krAsset + usAssetKrw;
    const totalPnl = btcPnl + krPnl + usPnlKrw;
    const totalPnlPct = totalAsset > 0 ? (totalPnl / (totalAsset - totalPnl || 1)) * 100 : 0;

    return [
      { label: "전체",     value: krw(totalAsset),       delta: totalPnlPct, pnlKrw: totalPnl,  accent: "var(--text-secondary)" },
      { label: "BTC",      value: krw(btcAsset),          delta: btcPnlPct,   pnlKrw: btcPnl,   accent: "var(--accent-btc)"    },
      { label: "국내 주식", value: krw(krAsset),           delta: krPnlPct,    pnlKrw: krPnl,    accent: "var(--accent-kr)"     },
      { label: "미국 주식", value: usd(usCurrentUsd),      delta: usPnlPct,    pnlKrw: usPnlKrw, accent: "var(--accent-us)"     },
    ];
  }, [btc, kr, us, fx]);

  const currentPage = NAV.find((n) => {
    if (n.to === "/") return location.pathname === "/";
    return location.pathname.startsWith(n.to);
  });

  return (
    <div className="flex min-h-screen bg-[color:var(--bg-primary)]">
      <Sidebar
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        summary={summary}
        health={health}
        updatedAt={updatedAt}
        now={now}
      />

      {/* 메인 영역 */}
      <div className="flex min-w-0 flex-1 flex-col lg:ml-[220px]">
        {/* 상단 바 (모바일 햄버거 + 페이지 타이틀) */}
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-[color:var(--border-subtle)] bg-[rgba(10,10,15,0.92)] px-4 backdrop-blur-xl">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              className="grid h-9 w-9 place-items-center rounded-xl border border-white/10 bg-white/[0.03] text-[color:var(--text-secondary)] hover:text-white lg:hidden"
            >
              <Menu size={16} />
            </button>
            <div className="flex items-center gap-2">
              {currentPage && (
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: currentPage.accent, boxShadow: `0 0 8px ${currentPage.accent}` }}
                />
              )}
              <h1 className="text-sm font-semibold text-[color:var(--text-primary)]">
                {currentPage?.label || "대시보드"}
              </h1>
              {currentPage?.sub && (
                <span className="hidden text-xs text-[color:var(--text-muted)] sm:inline">{currentPage.sub}</span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* 헬스 상태 (데스크탑) */}
            <div className="hidden items-center gap-2 rounded-full border border-white/10 bg-white/[0.02] px-3 py-1.5 text-xs text-[color:var(--text-secondary)] md:flex">
              <HealthDot health={health} />
              <span>시스템 {health?.status === "ok" ? "정상" : health?.status === "degraded" ? "주의" : "확인 중"}</span>
            </div>
            <div className="hidden rounded-full border border-white/10 bg-white/[0.02] px-3 py-1.5 text-xs text-[color:var(--text-muted)] md:block">
              갱신 <span className="numeric">{relativeTime(updatedAt, now)}</span>
            </div>
            <button
              type="button"
              className="grid h-8 w-8 place-items-center rounded-full border border-white/10 bg-white/[0.02] text-[color:var(--text-secondary)] hover:text-white"
            >
              <Settings2 size={14} />
            </button>
          </div>
        </header>

        {/* 페이지 콘텐츠 */}
        <main className="flex-1 px-3 pb-6 pt-3 lg:px-5">
          <AnimatePresence mode="wait">
            <motion.div
              key={location.pathname}
              variants={pageVariants}
              initial="initial"
              animate="animate"
              exit="exit"
            >
              {outlet}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}
