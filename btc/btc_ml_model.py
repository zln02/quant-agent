#!/usr/bin/env python3
"""BTC ML 모델 (PR #25 신규).

KR stocks/ml_model.py 패턴 미러. 단순화:
- pyupbit 1시간봉 (최근 N봉) → 학습 데이터
- features: RSI/BB/Vol/Momentum (BTC 특화 F&G/funding은 v2에서)
- target: 다음 K봉 후 +R% 이상 = 1 (Triple Barrier 적용)
- xgb only (앙상블은 KR 메인 라인에서)

학습 후 brain/ml/btc/{xgb_model.ubj, performance.json, feature_baseline.npz} 생성.
predict_btc(): 최신 시점 매수 확률 반환.

신규 인프라 — 사용자 train 트리거 필요:
    .venv/bin/python -m btc.btc_ml_model train

== 알려진 한계 (후속 PR) ==
- (#1) Triple Barrier 같은 봉에 TP/SL 둘 다 닿으면 SL 우선 — 분봉 없는 1h 데이터 한계
- (#2) Vertical barrier 만료 시 close 부호로 약 라벨 — 학습 신호 약함
- (#6) predict_btc 호출마다 2000봉 fetch — 캐시 미적용
- (#8) F&G/funding/OI/whale 등 BTC 특화 피처 미반영 — AUC 0.55 한계의 주 원인
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pyupbit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config import \
    BRAIN_PATH  # noqa: E402  # 단일 진실 — workspace/brain 고정
from common.env_loader import load_env  # noqa: E402

load_env()

MODEL_DIR = BRAIN_PATH / "ml" / "btc"
MODEL_PATH = MODEL_DIR / "xgb_model.ubj"
PERF_PATH = MODEL_DIR / "performance.json"
DRIFT_PATH = MODEL_DIR / "drift_report.json"
# PR #25 hotfix: 학습 시점 feature 분포 baseline — drift 비교의 진짜 기준
BASELINE_PATH = MODEL_DIR / "feature_baseline.npz"

# 학습 파라미터 (BTC 변동성 고려)
INTERVAL = "minute60"     # 1시간봉
LOOKBACK_COUNT = 2000     # 약 83일치 1시간봉
TARGET_HORIZON = 6        # 6시간 후 평가
TARGET_RETURN = 0.02      # +2% / -2% 배리어
FEATURE_LOOKBACK = 24     # 24시간 윈도우


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0).sum() / period
    losses = -np.where(deltas < 0, deltas, 0).sum() / period
    if losses <= 0:
        return 100.0
    rs = gains / losses
    return float(100 - (100 / (1 + rs)))


def _bb_position(closes: np.ndarray, period: int = 20) -> float:
    """볼린저밴드 내 상대 위치 [0,1]. 0=하단, 1=상단."""
    if len(closes) < period:
        return 0.5
    seg = closes[-period:]
    mean = seg.mean()
    std = seg.std()
    if std <= 0:
        return 0.5
    upper = mean + 2 * std
    lower = mean - 2 * std
    return float(np.clip((closes[-1] - lower) / max(upper - lower, 1e-9), 0.0, 1.0))


def _vol_ratio(volumes: np.ndarray, short: int = 5, long: int = 24) -> float:
    if len(volumes) < long:
        return 1.0
    short_avg = volumes[-short:].mean()
    long_avg = volumes[-long:].mean()
    return float(short_avg / max(long_avg, 1e-9))


def _momentum(closes: np.ndarray, lookback: int) -> float:
    if len(closes) < lookback + 1:
        return 0.0
    return float((closes[-1] - closes[-(lookback + 1)]) / max(closes[-(lookback + 1)], 1e-9))


def extract_features(closes: np.ndarray, volumes: np.ndarray, highs: np.ndarray, lows: np.ndarray, idx: int) -> list | None:
    """BTC 1시간봉 idx 시점 피처. idx는 학습 시 과거 시점, 추론 시 len-1."""
    if idx < FEATURE_LOOKBACK:
        return None
    c = closes[: idx + 1]
    v = volumes[: idx + 1]
    h = highs[: idx + 1]
    lo = lows[: idx + 1]
    return [
        _rsi(c, 14),
        _rsi(c, 21),
        _bb_position(c, 20),
        _vol_ratio(v, 5, 24),
        _vol_ratio(v, 12, 48),
        _momentum(c, 6),       # 6h
        _momentum(c, 24),      # 24h
        _momentum(c, 72),      # 72h
        float(np.std(c[-24:]) / max(np.mean(c[-24:]), 1e-9)),  # 24h CV
        float((h[-1] - lo[-1]) / max(c[-1], 1e-9)),            # 마지막 봉 range
    ]


FEATURE_NAMES = [
    "rsi_14", "rsi_21", "bb_pos", "vol_ratio_5_24", "vol_ratio_12_48",
    "mom_6h", "mom_24h", "mom_72h", "cv_24h", "bar_range",
]


def _triple_barrier_label(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, i: int) -> int:
    """KR ml_model 동일 패턴. SL 우선 (보수적)."""
    entry = closes[i]
    tp = entry * (1.0 + TARGET_RETURN)
    sl = entry * (1.0 - TARGET_RETURN)
    for step in range(1, TARGET_HORIZON + 1):
        idx = i + step
        if idx >= len(closes):
            break
        if lows[idx] <= sl:
            return 0
        if highs[idx] >= tp:
            return 1
    final = closes[min(i + TARGET_HORIZON, len(closes) - 1)]
    return 1 if (final - entry) / max(entry, 1e-9) >= 0 else 0


def _fetch_ohlcv() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """pyupbit 1시간봉 LOOKBACK_COUNT개. 한 번에 200개 한계 → 페이지네이션."""
    chunks = []
    to_ts = None
    remaining = LOOKBACK_COUNT
    while remaining > 0:
        size = min(200, remaining)
        df = pyupbit.get_ohlcv("KRW-BTC", interval=INTERVAL, count=size, to=to_ts)
        if df is None or len(df) == 0:
            break
        chunks.append(df)
        remaining -= len(df)
        to_ts = df.index[0].strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(0.1)  # API rate
    if not chunks:
        return np.array([]), np.array([]), np.array([]), np.array([])
    import pandas as pd
    df_all = pd.concat(chunks).sort_index().drop_duplicates()
    closes = df_all["close"].to_numpy(dtype=float)
    volumes = df_all["volume"].to_numpy(dtype=float)
    highs = df_all["high"].to_numpy(dtype=float)
    lows = df_all["low"].to_numpy(dtype=float)
    return closes, volumes, highs, lows


def _build_training_dataset(closes: np.ndarray, volumes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for i in range(FEATURE_LOOKBACK, len(closes) - TARGET_HORIZON):
        feats = extract_features(closes, volumes, highs, lows, i)
        if feats is None:
            continue
        X.append(feats)
        y.append(_triple_barrier_label(closes, highs, lows, i))
    return np.array(X, dtype=float), np.array(y, dtype=int)


def train(verbose: bool = True) -> dict:
    """BTC ML 학습. 1시간봉 → xgb. brain/ml/btc/ 저장."""
    closes, volumes, highs, lows = _fetch_ohlcv()
    if len(closes) < 200:
        if verbose:
            print(f"데이터 부족: {len(closes)}봉 (최소 200 필요)")
        return {"ok": False, "reason": "data_insufficient"}

    X, y = _build_training_dataset(closes, volumes, highs, lows)
    if len(X) < 100:
        if verbose:
            print(f"학습 샘플 부족: {len(X)}")
        return {"ok": False, "reason": "samples_insufficient"}

    buys = int(y.sum())
    if verbose:
        print(f"BTC 학습 데이터: {len(X)}개 (매수 {buys} / 관망 {len(X) - buys})")

    split = int(len(X) * 0.8)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]
    pos = max(int(y_tr.sum()), 1)
    neg = max(len(y_tr) - pos, 1)
    scale_pos_weight = neg / pos

    try:
        import xgboost as xgb
    except ImportError:
        return {"ok": False, "reason": "xgboost_missing"}

    # PR #25 hotfix(#4): walk-forward CV — 시계열 5-fold, 단순 80/20 보강
    try:
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import TimeSeriesSplit
        wf_aucs = []
        tscv = TimeSeriesSplit(n_splits=5)
        for _fold, (_tr_i, _val_i) in enumerate(tscv.split(X_tr), 1):
            if len(set(y_tr[_val_i])) < 2:
                continue
            _m = xgb.XGBClassifier(
                n_estimators=200, max_depth=4, learning_rate=0.05,
                scale_pos_weight=scale_pos_weight, eval_metric="auc",
                random_state=42, n_jobs=2,
            )
            _m.fit(X_tr[_tr_i], y_tr[_tr_i], verbose=False)
            _p = _m.predict_proba(X_tr[_val_i])[:, 1]
            wf_aucs.append(float(roc_auc_score(y_tr[_val_i], _p)))
        if verbose and wf_aucs:
            print(f"walk-forward AUC: {[round(a,3) for a in wf_aucs]} mean={sum(wf_aucs)/len(wf_aucs):.3f}")
    except Exception as _wfe:
        wf_aucs = []
        if verbose:
            print(f"walk-forward 스킵: {_wfe}")

    model = xgb.XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        scale_pos_weight=scale_pos_weight, eval_metric="auc",
        random_state=42, n_jobs=2,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model.save_model(str(MODEL_PATH))
    # PR #25 hotfix(#5): 학습 시점 X_tr 분포를 baseline으로 저장 — drift 비교 기준
    np.savez_compressed(str(BASELINE_PATH), X_train=X_tr)

    # OOS 평가
    from sklearn.metrics import accuracy_score, roc_auc_score
    probs = model.predict_proba(X_te)[:, 1]
    preds = (probs >= 0.65).astype(int)
    acc = float(accuracy_score(y_te, preds))
    auc = float(roc_auc_score(y_te, probs)) if len(set(y_te)) > 1 else 0.0

    perf = {
        "train_samples": len(X_tr),
        "test_samples": len(X_te),
        "buy_ratio": round(buys / len(X), 3),
        "oos_accuracy": round(acc, 3),
        "oos_auc": round(auc, 3),
        "walk_forward_auc_mean": round(sum(wf_aucs) / len(wf_aucs), 3) if wf_aucs else None,
        "walk_forward_auc_folds": [round(a, 3) for a in wf_aucs],
        "features": FEATURE_NAMES,
        "interval": INTERVAL,
        "target_horizon": TARGET_HORIZON,
        "target_return": TARGET_RETURN,
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    PERF_PATH.write_text(json.dumps(perf, indent=2, ensure_ascii=False), encoding="utf-8")
    if verbose:
        print(f"OOS accuracy={acc:.3f}, AUC={auc:.3f}")
        print(f"저장: {MODEL_PATH}")
    return {"ok": True, **perf}


def _load_model():
    if not MODEL_PATH.exists():
        return None
    try:
        import xgboost as xgb
        model = xgb.XGBClassifier()
        model.load_model(str(MODEL_PATH))
        return model
    except Exception:
        return None


def predict_btc() -> dict:
    """최신 시점 BTC 매수 확률. 모델 없으면 source=NO_MODEL 반환."""
    model = _load_model()
    if model is None:
        return {"action": "HOLD", "confidence": 0.0, "source": "BTC_ML_NO_MODEL"}

    closes, volumes, highs, lows = _fetch_ohlcv()
    if len(closes) < FEATURE_LOOKBACK + 1:
        return {"action": "HOLD", "confidence": 0.0, "source": "BTC_ML_DATA_SHORT"}

    feats = extract_features(closes, volumes, highs, lows, len(closes) - 1)
    if feats is None:
        return {"action": "HOLD", "confidence": 0.0, "source": "BTC_ML_FEATURE_FAIL"}

    prob = float(model.predict_proba(np.array([feats], dtype=float))[0][1])
    return {
        "action": "BUY" if prob >= 0.65 else "HOLD",
        "confidence": round(prob * 100, 1),
        "source": "BTC_ML_XGB",
        "probability": round(prob, 4),
    }


def _psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index (KR ml_drift_monitor와 동일 패턴)."""
    expected = expected[np.isfinite(expected)]
    actual = actual[np.isfinite(actual)]
    if len(expected) < 20 or len(actual) < 10:
        return 0.0
    edges = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    exp_hist, _ = np.histogram(expected, bins=edges)
    act_hist, _ = np.histogram(actual, bins=edges)
    exp_pct = np.clip(exp_hist / max(exp_hist.sum(), 1), 1e-6, None)
    act_pct = np.clip(act_hist / max(act_hist.sum(), 1), 1e-6, None)
    return float(np.sum((act_pct - exp_pct) * np.log(act_pct / exp_pct)))


def build_drift_report(recent_samples: int = 200) -> dict:
    """BTC ML drift report (PR #25). 학습 시점 baseline vs 최근 분포 PSI."""
    import time
    if not PERF_PATH.exists():
        return {"status": "NO_MODEL", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    # PR #25 hotfix(#5): 학습 시점 X_tr 분포를 baseline.npz 에서 로드
    if not BASELINE_PATH.exists():
        return {"status": "NO_BASELINE",
                "hint": "재학습 필요 — train() 호출 시 feature_baseline.npz 생성됨",
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        train_feats = np.load(str(BASELINE_PATH))["X_train"]
    except Exception as _le:
        return {"status": "BASELINE_LOAD_FAIL", "error": str(_le),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    closes, volumes, highs, lows = _fetch_ohlcv()
    if len(closes) < FEATURE_LOOKBACK + recent_samples:
        return {"status": "INSUFFICIENT_DATA", "samples": len(closes),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    recent_feats, _ = _build_training_dataset(closes[-recent_samples:], volumes[-recent_samples:],
                                               highs[-recent_samples:], lows[-recent_samples:])
    if len(train_feats) == 0 or len(recent_feats) == 0:
        return {"status": "FEATURE_BUILD_FAIL",
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    rows = []
    max_psi = 0.0
    high_psi = 0
    for i, name in enumerate(FEATURE_NAMES):
        psi = _psi(train_feats[:, i], recent_feats[:, i])
        level = "stable"
        if psi >= 0.25:
            level = "danger"
            high_psi += 1
        elif psi >= 0.10:
            level = "warning"
        max_psi = max(max_psi, psi)
        rows.append({"feature": name, "psi": round(psi, 6), "level": level})
    rows.sort(key=lambda r: r["psi"], reverse=True)

    overall = "stable"
    action = "none"
    if max_psi >= 0.25:
        overall, action = "danger", "retrain"
    elif max_psi >= 0.10:
        overall, action = "warning", "alert"

    report = {
        "status": overall.upper(),
        "recommended_action": action,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "training_samples": int(len(train_feats)),
        "recent_samples": int(len(recent_feats)),
        "max_psi": round(max_psi, 6),
        "high_psi_count": int(high_psi),
        "top_drift_features": rows[:5],
        "all_features": rows,
    }
    DRIFT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DRIFT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "train":
        result = train(verbose=True)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "predict":
        print(json.dumps(predict_btc(), indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "drift":
        print(json.dumps(build_drift_report(), indent=2, ensure_ascii=False))
    else:
        print("Usage: python -m btc.btc_ml_model {train|predict|drift}")
