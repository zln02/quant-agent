import { DockviewReact } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import "../styles/dockview-theme.css";
import { useCallback, useRef } from "react";

import { ProDataProvider } from "./pro/ProDataContext";
import { load, save } from "./pro/proLayoutPersist";
import ChartPanel from "./pro/panels/ChartPanel";
import ScorePortfolioPanel from "./pro/panels/ScorePortfolioPanel";
import TradesPanel from "./pro/panels/TradesPanel";

const components = {
  chart: ChartPanel,
  score: ScorePortfolioPanel,
  trades: TradesPanel,
};

function buildDefaultLayout(api) {
  api.addPanel({ id: "chart", component: "chart", title: "차트" });
  api.addPanel({
    id: "score",
    component: "score",
    title: "점수 · 포트폴리오",
    position: { referencePanel: "chart", direction: "right" },
  });
  api.addPanel({
    id: "trades",
    component: "trades",
    title: "체결",
    position: { referencePanel: "chart", direction: "below" },
  });
}

export default function ProPage() {
  const apiRef = useRef(null);

  const onReady = useCallback((event) => {
    const { api } = event;
    apiRef.current = api;

    let restored = false;
    const saved = load();
    if (saved) {
      try {
        api.fromJSON(saved);
        restored = api.panels.length > 0;
      } catch (_) {
        restored = false;
      }
    }
    if (!restored) {
      buildDefaultLayout(api);
    }

    api.onDidLayoutChange(() => {
      try {
        save(api.toJSON());
      } catch (_) {
        // ignore serialization failure
      }
    });
  }, []);

  const handleReset = () => {
    const api = apiRef.current;
    if (!api) return;
    api.clear();
    buildDefaultLayout(api);
  };

  return (
    <ProDataProvider>
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-[color:var(--text-primary)]">
              Pro 워크스페이스
            </h2>
            <p className="text-[11px] text-[color:var(--text-muted)]">
              패널을 드래그·탭화해서 자유롭게 재배치할 수 있어. 레이아웃은 자동 저장됨.
            </p>
          </div>
          <button
            type="button"
            onClick={handleReset}
            className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-[11px] text-[color:var(--text-secondary)] hover:text-white"
          >
            레이아웃 초기화
          </button>
        </div>
        <div className="dockview-theme-quant overflow-hidden rounded-[var(--panel-radius)] border border-white/10 shadow-[var(--shadow-panel)] h-[calc(100vh-160px)] min-h-[480px]">
          <DockviewReact components={components} onReady={onReady} />
        </div>
      </div>
    </ProDataProvider>
  );
}
