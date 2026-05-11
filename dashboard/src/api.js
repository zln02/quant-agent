const API_BASE = import.meta.env.VITE_API_URL || "";

async function fetchJSON(path, timeoutMs = 10000, retries = 1) {
  for (let i = 0; i <= retries; i++) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(`${API_BASE}${path}`, { signal: controller.signal });
      clearTimeout(timeoutId);
      if (res.status === 429) {
        await new Promise((r) => setTimeout(r, 2000));
        continue;
      }
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    } catch (e) {
      clearTimeout(timeoutId);
      if (i === retries) throw e;
    }
  }
}

/** 네트워크/API 오류 시 null 반환 (폴링에 유용) */
async function fetchJSONSafe(path) {
  let networkRetryUsed = false;

  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      return await fetchJSON(path);
    } catch (error) {
      const message = String(error?.message || error || "");
      const is429 = message.includes("429");
      const isNetworkError =
        error?.name === "AbortError" ||
        message.includes("Failed to fetch") ||
        message.includes("NetworkError");

      if (is429 && attempt < 2) {
        const wait = Math.min(2000 * Math.pow(2, attempt), 16000);
        await new Promise((resolve) => setTimeout(resolve, wait));
        continue;
      }

      if (isNetworkError && !networkRetryUsed) {
        networkRetryUsed = true;
        await new Promise((resolve) => setTimeout(resolve, 1000));
        continue;
      }

      return null;
    }
  }

  return null;
}

// BTC
export const getBtcComposite = () => fetchJSON("/api/btc/composite");
export const getBtcPortfolio = () => fetchJSON("/api/btc/portfolio");
export const getBtcStats = () => fetchJSON("/api/stats");
export const getBtcTrades = () => fetchJSON("/api/trades?limit=20");
export const getBtcDecisionLog = (limit = 20) =>
  fetchJSON(`/api/btc/decision-log?limit=${limit}`);
export const getBtcLogs = () => fetchJSON("/api/logs");
export const getBtcNews = () => fetchJSON("/api/news");
export const getBtcSystem = () => fetchJSON("/api/system");
export const getBtcCandles = (interval = "minute5", count = 100) =>
  fetchJSON(`/api/candles?interval=${interval}&count=${count}`);
export const getBtcRealtimeNews = (currencies = "BTC", limit = 10) =>
  fetchJSONSafe(`/api/realtime/news?currencies=${encodeURIComponent(currencies)}&limit=${limit}`);
export const getBtcRealtimeOrderbook = (market = "upbit", symbol = "KRW-BTC") =>
  fetchJSONSafe(`/api/realtime/orderbook?market=${encodeURIComponent(market)}&symbol=${encodeURIComponent(symbol)}`);
export const getBtcRealtimeAlt = (symbol = "BTC") =>
  fetchJSONSafe(`/api/realtime/alt/${encodeURIComponent(symbol)}`);
export const getBtcRealtimePrice = (symbol = "KRW-BTC", market = "btc") =>
  fetchJSONSafe(`/api/realtime/price/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}`);
export const getBtcFilters = () => fetchJSONSafe("/api/btc/filters");

// KR Stocks
export const getKrComposite = () => fetchJSONSafe("/api/kr/composite");
export const getKrPortfolio = () => fetchJSON("/api/kr/portfolio");
export const getKrSystem = () => fetchJSONSafe("/api/kr/system");
export const getKrTop = () => fetchJSONSafe("/api/kr/top");
export const getKrTrades = (limit = 50, action = null, hours = null) => {
  let url = `/api/kr/trades?limit=${limit}`;
  if (action) url += `&action=${encodeURIComponent(action)}`;
  if (hours) url += `&hours=${hours}`;
  return fetchJSONSafe(url);
};
export const getKrPositions = () => fetchJSONSafe("/api/kr/positions");
export const getKrDailyPnl = (days = 7) => fetchJSONSafe(`/api/stocks/daily-pnl?days=${days}`);
export const getStockOverview = () => fetchJSON("/api/stocks/overview");
export const getStockPortfolio = () => fetchJSON("/api/stocks/portfolio");
export const getStockTrades = () => fetchJSON("/api/stocks/trades?limit=20");
export const getStockMarket = () => fetchJSON("/api/stocks/market-summary");
export const getStockStrategy = () => fetchJSON("/api/stocks/strategy");
export const getStockChart = (code, interval = "1d", limit = 65) =>
  fetchJSONSafe(`/api/stocks/chart/${encodeURIComponent(code)}?interval=${interval}&limit=${limit}`);
export const getStockIndicators = (code) =>
  fetchJSONSafe(`/api/stocks/indicators/${encodeURIComponent(code)}`);
export const getStockLogs = (source = "all") => fetchJSONSafe(`/api/stocks/logs?source=${source}`);
export const getStockRealtimePrice = (code) => fetchJSONSafe(`/api/stocks/realtime/price/${encodeURIComponent(code)}`);
export const getStockRealtimeOrderbook = (code) => fetchJSONSafe(`/api/stocks/realtime/orderbook/${encodeURIComponent(code)}`);
export const getStockRealtimeAlt = (symbol) => fetchJSONSafe(`/api/stocks/realtime/alt/${encodeURIComponent(symbol)}`);

// US Stocks
export const getUsComposite = () => fetchJSONSafe("/api/us/composite");
export const getUsPositions = () => fetchJSON("/api/us/positions");
export const getUsTrades = () => fetchJSON("/api/us/trades");
export const getUsMarket = () => fetchJSON("/api/us/market");
export const getUsFx = () => fetchJSONSafe("/api/us/fx");
export const getUsSystem = () => fetchJSONSafe("/api/us/system");
export const getUsChart = (symbol, period = "3mo", interval = "1d") =>
  fetchJSONSafe(`/api/us/chart/${encodeURIComponent(symbol)}?period=${encodeURIComponent(period)}&interval=${encodeURIComponent(interval)}`);
export const getUsRealtimeNews = (symbol = "AAPL", limit = 10) =>
  fetchJSONSafe(`/api/us/realtime/news?symbol=${encodeURIComponent(symbol)}&limit=${limit}`);
export const getUsRealtimePrice = (symbol) => fetchJSONSafe(`/api/us/realtime/price/${encodeURIComponent(symbol)}`);
export const getUsRealtimeAlt = (symbol) => fetchJSONSafe(`/api/us/realtime/alt/${encodeURIComponent(symbol)}`);

export const getRiskMetrics = () => fetchJSONSafe("/api/risk-metrics");

// Health
export const getHealth = () => fetchJSON("/health");
