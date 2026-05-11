#!/usr/bin/env python3
"""
키움증권 REST API 클라이언트 v2.0

변경사항 (v1 → v2):
- [FIX] place_order 응답 검증 추가 (return_code 체크)
- [FIX] get_stock_info를 _call_api 통합
- [NEW] get_current_price() 메서드 추가
- [NEW] API 호출 재시도 로직 (네트워크 오류 대응)
- [NEW] 토큰 만료 시간을 서버 응답 기준으로
- [NEW] 요청 속도 제한 (rate limiting)
- [REFACTOR] 로깅 강화, 타입 힌트 정리

필수 환경 변수:
    TRADING_ENV=mock
    KIWOOM_REST_API_KEY=your_key
    KIWOOM_REST_API_SECRET=your_secret
    KIWOOM_ACCOUNT_NO=5012345678
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional

try:
    from dotenv import load_dotenv
    _has_dotenv = True
except ImportError:
    _has_dotenv = False
    load_dotenv = None  # type: ignore

import httpx

try:
    import sys as _sys
    _ws = str(Path(__file__).resolve().parents[1])
    if _ws not in _sys.path:
        _sys.path.insert(0, _ws)
    from common.env_loader import load_env as _load_common_env
    from common.logger import get_logger as _get_logger
    _kiwoom_log = _get_logger("kiwoom_client")
    def _log(msg: str, level: str = "INFO"):
        level_upper = level.upper()
        if level_upper in ("TRADE",):
            _kiwoom_log.info(f"[TRADE] {msg}")
        elif level_upper == "WARN":
            _kiwoom_log.warning(msg)
        elif level_upper == "ERROR":
            _kiwoom_log.error(msg)
        elif level_upper == "DEBUG":
            _kiwoom_log.debug(msg)
        else:
            _kiwoom_log.info(msg)
except Exception:
    _load_common_env = None  # type: ignore
    import logging as _logging
    _kiwoom_log = _logging.getLogger("kiwoom_client")
    def _log(msg: str, level: str = "INFO"):
        level_upper = level.upper()
        if level_upper in ("TRADE",):
            _kiwoom_log.info(f"[TRADE] {msg}")
        elif level_upper == "WARN":
            _kiwoom_log.warning(msg)
        elif level_upper == "ERROR":
            _kiwoom_log.error(msg)
        else:
            _kiwoom_log.info(msg)


def _int(v) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def _float(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _load_env_from_file(env_path: Path) -> bool:
    if not _has_dotenv or load_dotenv is None:
        return False
    if not env_path.exists():
        return False
    load_dotenv(env_path)
    return True


def find_project_root() -> Path:
    current = Path(__file__).resolve().parent
    search = current
    for _ in range(10):
        if (search / ".env").exists():
            return search
        parent = search.parent
        if parent == search:
            break
        search = parent
    search = current
    for _ in range(10):
        if (search / ".env.example").exists():
            return search
        parent = search.parent
        if parent == search:
            break
        search = parent
    return Path.cwd()


# ---- Order strategy mapping ----
# 키움 REST API kt10000(매수)/kt10001(매도) body의 두 필드 쌍을 추상화.
# - trde_tp: 거래구분 (0=보통/지정가, 3=시장가)
# - ord_prc_ptn_cd: 호가구분 (00=일반, 03=시장가)
#
# 두 필드는 한 쌍으로 동시 셋팅되어야 함. STRATEGY_MAP은 키움 REST
# 스펙의 매직 코드를 의미 있는 이름으로 추상화하여 호출자 의도를 명시.
#
# IOC/FOK는 키움 REST 공식 문서 확인 후 PR #9b에서 추가 예정.
STRATEGY_MAP: Dict[str, Dict[str, str]] = {
    "LIMIT":  {"trde_tp": "0", "ord_prc_ptn_cd": "00"},
    "MARKET": {"trde_tp": "3", "ord_prc_ptn_cd": "03"},
}

OrderStrategy = Literal["LIMIT", "MARKET"]


# ─────────────────────────────────────────────
# 메인 클라이언트
# ─────────────────────────────────────────────
class KiwoomAPIClient:
    """키움증권 REST API 클라이언트 v2"""

    MIN_REQUEST_INTERVAL = 0.6  # 429 완화

    def __init__(self, use_mock: Optional[bool] = None):
        if _load_common_env is not None:
            _load_common_env()
        project_root = find_project_root()
        env_path = project_root / ".env"
        _load_env_from_file(env_path)

        # openclaw 환경에서 단독 실행 시 env 보강
        _openclaw = Path("/home/wlsdud5035/.openclaw/openclaw.json")
        if _openclaw.exists():
            try:
                data = json.loads(_openclaw.read_text())
                for k, v in (data.get("env") or {}).items():
                    if isinstance(v, str) and k.startswith("KIWOOM"):
                        os.environ.setdefault(k, v)
            except Exception:
                pass

        if use_mock is None:
            trading_env = os.getenv("TRADING_ENV", "mock").lower()
            use_mock = (trading_env == "mock")

        self.api_key = (
            os.getenv("KIWOOM_REST_API_KEY")
            or os.getenv("KIWOOM_MOCK_REST_API_APP_KEY")
        )
        self.api_secret = (
            os.getenv("KIWOOM_REST_API_SECRET")
            or os.getenv("KIWOOM_MOCK_REST_API_SECRET_KEY")
        )
        self.account_no = (
            os.getenv("KIWOOM_ACCOUNT_NO")
            or os.getenv("KIWOOM_MOCK_ACCOUNT_NO")
        )

        if not self.api_key or not self.api_secret:
            raise ValueError(
                "API Key/Secret이 설정되지 않았습니다.\n"
                "KIWOOM_REST_API_KEY, KIWOOM_REST_API_SECRET 환경변수를 설정하세요."
            )

        self.base_url = (
            "https://mockapi.kiwoom.com" if use_mock
            else "https://api.kiwoom.com"
        )
        self.use_mock = use_mock

        self.token = None
        self.token_expires = 0.0
        self._last_request_time = 0.0

        _log(f"초기화 완료: {'모의투자' if use_mock else '실전투자'} ({self.base_url})")

    def _get_token(self) -> str:
        """OAuth 토큰 발급 (캐시, 자동 갱신)"""
        if self.token and time.time() < (self.token_expires - 300):
            return self.token

        url = f"{self.base_url}/oauth2/token"
        data = {
            "grant_type": "client_credentials",
            "appkey": self.api_key,
            "secretkey": self.api_secret,
        }

        try:
            response = httpx.post(url, json=data, timeout=30.0)
            result = response.json()
        except Exception as e:
            raise Exception(f"토큰 발급 네트워크 오류: {e}")

        if result.get('return_code') != 0:
            raise Exception(f"토큰 발급 실패: {result.get('return_msg')}")

        self.token = result['token']
        expires_in = int(result.get('expires_in', 3600))
        self.token_expires = time.time() + expires_in

        _log(f"토큰 갱신 완료 (만료: {expires_in}초)")
        return self.token

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.MIN_REQUEST_INTERVAL:
            time.sleep(self.MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.time()

    def _call_api(
        self,
        api_id: str,
        endpoint: str,
        body: dict,
        extra_headers: dict = None,
        retries: int = 2,
    ) -> Dict:
        last_error = None

        for attempt in range(retries + 1):
            try:
                self._rate_limit()
                token = self._get_token()

                url = f"{self.base_url}{endpoint}"
                headers = {
                    "Content-Type": "application/json;charset=UTF-8",
                    "api-id": api_id,
                    "authorization": f"Bearer {token}",
                }
                if extra_headers:
                    headers.update(extra_headers)

                response = httpx.post(url, headers=headers, json=body, timeout=30.0)

                if response.status_code == 429:
                    wait = min(2 * (2 ** attempt), 30)
                    _log(f"[{api_id}] Rate Limit (429) → {wait}초 대기 후 재시도", "WARN")
                    time.sleep(wait)
                    continue
                if response.status_code != 200:
                    raise Exception(
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )

                result = response.json()

                return_code = result.get("return_code")
                if return_code != 0:
                    error_msg = result.get("return_msg", "알 수 없는 오류")
                    if return_code in (-100, -101, 401):
                        _log("토큰 만료 감지 → 갱신 후 재시도", "WARN")
                        self.token = None
                        continue
                    raise Exception(
                        f"[{api_id}] API 오류 (code={return_code}): {error_msg}"
                    )

                return result

            except Exception as e:
                last_error = e
                if attempt < retries:
                    wait = min(2 * (2 ** attempt), 30)
                    _log(f"[{api_id}] 재시도 {attempt+1}/{retries} ({wait}초 후): {e}", "WARN")
                    time.sleep(wait)

        raise Exception(f"[{api_id}] {retries}회 재시도 후 최종 실패: {last_error}")

    def get_stock_info(self, stock_code: str) -> Optional[Dict]:
        """주식 기본 정보 조회 (ka10001)"""
        result = self._call_api("ka10001", "/api/dostk/stkinfo", {
            "stk_cd": stock_code,
        })
        return result

    def get_investor_trend(self, stock_code: str) -> Dict:
        """투자자별 매매동향 조회 (외국인/기관/개인)"""
        try:
            result = self._call_api("ka10007", "/api/dostk/stkinfo", {
                "stk_cd": stock_code,
            })
            output = result.get('output', result)
            return {
                "foreign_net": _int(output.get("frgn_net_buy_qty", 0)),
                "inst_net": _int(output.get("orgn_net_buy_qty", 0)),
                "individual_net": _int(output.get("indv_net_buy_qty", 0)),
                "foreign_ratio": _float(output.get("frgn_hold_rt", 0)),
            }
        except Exception as e:
            _log(f"투자자 동향 조회 실패 {stock_code}: {e}", "WARN")
            return {}

    def get_current_price(self, stock_code: str) -> int:
        """현재가만 간편 조회"""
        try:
            info = self.get_stock_info(stock_code)
            if not info:
                return 0
            output = info.get('output', info)
            price_str = (
                output.get('stck_prpr')
                or output.get('cur_prc')
                or '0'
            )
            return abs(int(str(price_str).replace(',', '')))
        except Exception as e:
            _log(f"현재가 조회 실패 {stock_code}: {e}", "ERROR")
            return 0

    def get_account_evaluation(self) -> Dict:
        """계좌평가현황 조회 (kt00004)"""
        result = self._call_api("kt00004", "/api/dostk/acnt", {
            "qry_tp": "0",
            "dmst_stex_tp": "KRX",
        })

        summary = {
            "deposit": _int(result.get("entr")),
            "d2_deposit": _int(result.get("d2_entra")),
            "total_evaluation": _int(result.get("tot_est_amt")),
            "total_asset": _int(result.get("aset_evlt_amt")),
            "total_purchase": _int(result.get("tot_pur_amt")),
            "estimated_asset": _int(result.get("prsm_dpst_aset_amt")),
            "today_pnl": _int(result.get("tdy_lspft")),
            "monthly_pnl": _int(result.get("lspft2")),
            "cumulative_pnl": _int(result.get("lspft")),
            "today_pnl_pct": _float(result.get("tdy_lspft_rt")),
            "monthly_pnl_pct": _float(result.get("lspft_ratio")),
            "cumulative_pnl_pct": _float(result.get("lspft_rt")),
        }

        holdings = []
        for s in result.get("stk_acnt_evlt_prst", []):
            holdings.append({
                "code": s.get("stk_cd", ""),
                "name": s.get("stk_nm", ""),
                "quantity": _int(s.get("rmnd_qty")),
                "avg_price": _int(s.get("avg_prc")),
                "current_price": abs(_int(s.get("cur_prc"))),
                "evaluation": _int(s.get("evlt_amt")),
                "pnl_amount": _int(s.get("pl_amt")),
                "pnl_pct": _float(s.get("pl_rt")),
                "purchase_amount": _int(s.get("pur_amt")),
            })

        return {"summary": summary, "holdings": holdings, "raw": result}

    def get_daily_balance_pnl(self, query_date: Optional[str] = None) -> Dict:
        """일별잔고수익률 조회 (ka01690)"""
        from datetime import date as _date
        if query_date is None:
            query_date = _date.today().strftime("%Y%m%d")

        result = self._call_api("ka01690", "/api/dostk/acnt", {
            "qry_dt": query_date,
        })

        summary = {
            "total_purchase": _int(result.get("tot_buy_amt")),
            "total_evaluation": _int(result.get("tot_evlt_amt")),
            "total_pnl": _int(result.get("tot_evltv_prft")),
            "total_pnl_pct": _float(result.get("tot_prft_rt")),
            "deposit": _int(result.get("dbst_bal")),
            "estimated_asset": _int(result.get("day_stk_asst")),
            "cash_ratio": _float(result.get("buy_wght")),
        }

        holdings = []
        for s in result.get("day_bal_rt", []):
            holdings.append({
                "code": s.get("stk_cd", ""),
                "name": s.get("stk_nm", ""),
                "current_price": abs(_int(s.get("cur_prc"))),
                "quantity": _int(s.get("rmnd_qty")),
                "avg_price": _int(s.get("buy_uv")),
                "buy_ratio": _float(s.get("buy_wght")),
                "evaluation": _int(s.get("evlt_amt")),
                "eval_ratio": _float(s.get("evlt_wght")),
                "pnl_amount": _int(s.get("evltv_prft")),
                "pnl_pct": _float(s.get("prft_rt")),
            })

        return {
            "date": result.get("dt", query_date),
            "summary": summary,
            "holdings": holdings,
            "raw": result,
        }

    def get_settlement_balance(self, exchange: str = "KRX") -> Dict:
        """체결잔고 조회 (kt00005) — 실전투자 전용"""
        if self.use_mock:
            raise Exception(
                "[kt00005] 모의투자 미지원. kt00004 (get_account_evaluation)를 사용하세요."
            )

        result = self._call_api("kt00005", "/api/dostk/acnt", {
            "dmst_stex_tp": exchange,
        })

        summary = {
            "deposit": _int(result.get("entr")),
            "deposit_d1": _int(result.get("entr_d1")),
            "deposit_d2": _int(result.get("entr_d2")),
            "orderable_cash": _int(result.get("ord_alowa")),
            "withdrawable": _int(result.get("pymn_alow_amt")),
            "unsettled_cash": _int(result.get("ch_uncla")),
            "total_buy_amount": _int(result.get("stk_buy_tot_amt")),
            "total_evaluation": _int(result.get("evlt_amt_tot")),
            "total_pnl": _int(result.get("tot_pl_tot")),
            "total_pnl_pct": _float(result.get("tot_pl_rt")),
            "substitute_amount": _int(result.get("repl_amt")),
            "credit_collateral_rate": _float(result.get("crd_grnt_rt")),
        }

        holdings = []
        for s in result.get("stk_cntr_remn", []):
            holdings.append({
                "code": s.get("stk_cd", ""),
                "name": s.get("stk_nm", ""),
                "settlement_balance": _int(s.get("setl_remn")),
                "current_quantity": _int(s.get("cur_qty")),
                "current_price": abs(_int(s.get("cur_prc"))),
                "avg_price": _int(s.get("buy_uv")),
                "purchase_amount": _int(s.get("pur_amt")),
                "evaluation": _int(s.get("evlt_amt")),
                "pnl_amount": _int(s.get("evltv_prft")),
                "pnl_pct": _float(s.get("pl_rt")),
                "credit_type": s.get("crd_tp", ""),
                "loan_date": s.get("loan_dt", ""),
                "expire_date": s.get("expr_dt", ""),
            })

        return {"summary": summary, "holdings": holdings, "raw": result}

    def place_order(
        self,
        stock_code: str,
        order_type: str,
        quantity: int,
        price: int = 0,
        market: str = "KRX",
        *,
        order_strategy: Optional[OrderStrategy] = None,
    ) -> Dict:
        """
        주식 주문 (buy/sell). 실패 시 예외 발생.

        Args:
            stock_code: 종목 코드 (국내: '005930', 해외/기타: 'AAPL_NX' 등)
            order_type: 'buy' 또는 'sell'
            quantity: 주문 수량
            price: 지정가 (LIMIT 시 가격, MARKET 시 0)
            market: 거래소 구분 (기본 'KRX', 필요 시 문서 기준으로 'NXT', 'SOR' 등)
            order_strategy: 'LIMIT' / 'MARKET' (keyword-only). None이면 price 기반 자동 추론
                — price == 0 → MARKET, price > 0 → LIMIT. 100% 하위 호환.

        호출 패턴:
            place_order(code, 'buy', qty)                                  # MARKET 자동
            place_order(code, 'buy', qty, price=70000)                     # LIMIT 자동
            place_order(code, 'sell', qty, price=p, order_strategy='LIMIT')  # 명시
        """
        if order_type not in ("buy", "sell"):
            raise ValueError(f"order_type은 'buy' 또는 'sell': {order_type}")
        if quantity < 1:
            raise ValueError("수량은 1 이상이어야 합니다.")

        if order_strategy is None:
            order_strategy = "MARKET" if price == 0 else "LIMIT"
        if order_strategy not in STRATEGY_MAP:
            raise ValueError(f"unknown order_strategy: {order_strategy}")
        strategy = STRATEGY_MAP[order_strategy]

        # 키움 REST API 주문: api-id는 kt10000(매수)/kt10001(매도). tr_id 헤더는 사용 안 함.
        api_id = "kt10000" if order_type == "buy" else "kt10001"

        acnt_no = (self.account_no or "")[:8]
        acnt_prdt_cd = (self.account_no or "")[8:] if len(self.account_no or "") > 8 else "01"

        body = {
            "dmst_stex_tp": market,
            "acnt_no": acnt_no,
            "acnt_prdt_cd": acnt_prdt_cd,
            "stk_cd": stock_code,
            "ord_qty": str(quantity),
            "ord_uv": str(price),
            "trde_tp": strategy["trde_tp"],
            "ord_prc_ptn_cd": strategy["ord_prc_ptn_cd"],
        }

        extra_headers = {
            "appkey": self.api_key,
            "secretkey": self.api_secret,
        }

        _log(
            f"주문 요청: {order_type.upper()} {stock_code} "
            f"{quantity}주 {'시장가' if price == 0 else f'{price:,}원'} "
            f"strategy={order_strategy} (api-id={api_id})",
            "TRADE",
        )

        result = self._call_api(
            api_id=api_id,
            endpoint="/api/dostk/ordr",
            body=body,
            extra_headers=extra_headers,
            retries=0,  # 주문 재시도 없음 — 네트워크 타임아웃 후 재시도 시 이중주문 위험
        )

        order_no = result.get("ord_no", result.get("odno", ""))
        success = bool(order_no)

        if success:
            _log(f"주문 성공: 주문번호 {order_no}", "TRADE")
        else:
            _log(f"주문 응답 이상: {result}", "WARN")
            try:
                from common.telegram import send_telegram
                send_telegram(f"⚠️ Kiwoom 주문 실패\n응답: {result.get('return_msg', str(result)[:100])}")
            except Exception:
                pass

        return {
            "success": success,
            "order_no": str(order_no),
            "message": result.get("return_msg", ""),
            "raw": result,
        }

    def get_asset_summary(self) -> Dict:
        """현재 자산 상태 요약"""
        data = self.get_account_evaluation()
        s = data["summary"]
        return {
            "environment": "모의투자" if self.use_mock else "실전투자",
            "deposit": s["deposit"],
            "estimated_asset": s["estimated_asset"],
            "total_purchase": s["total_purchase"],
            "total_evaluation": s["total_evaluation"],
            "cumulative_pnl": s["cumulative_pnl"],
            "cumulative_pnl_pct": s["cumulative_pnl_pct"],
            "holdings_count": len(data["holdings"]),
            "holdings": data["holdings"],
        }

    def get_environment_info(self) -> Dict:
        """현재 환경 정보"""
        return {
            "use_mock": self.use_mock,
            "base_url": self.base_url,
            "env_label": "모의투자" if self.use_mock else "실전투자",
            "account_no": self.account_no,
        }


KiwoomClient = KiwoomAPIClient


if __name__ == "__main__":
    client = KiwoomAPIClient()

    env_info = client.get_environment_info()
    print(f"환경: {env_info['env_label']} ({env_info['base_url']})")
    print()

    print("=" * 50)
    print("자산 현황")
    print("=" * 50)
    summary = client.get_asset_summary()
    print(f"  환경: {summary['environment']}")
    print(f"  예수금: {summary['deposit']:,}원")
    print(f"  추정예탁자산: {summary['estimated_asset']:,}원")
    print(f"  총매입금액: {summary['total_purchase']:,}원")
    print(f"  유가잔고평가: {summary['total_evaluation']:,}원")
    print(f"  누적손익: {summary['cumulative_pnl']:+,}원 ({summary['cumulative_pnl_pct']:+.2f}%)")
    print(f"  보유종목: {summary['holdings_count']}개")

    if summary["holdings"]:
        print()
        print("  보유종목 상세:")
        for h in summary["holdings"]:
            print(
                f"    {h['name']} ({h['code']})"
                f" | {h['quantity']}주"
                f" | 평가: {h['evaluation']:,}원"
                f" | 손익: {h['pnl_pct']:+.2f}%"
            )

    print()
    print("현재가 테스트:")
    price = client.get_current_price("005930")
    print(f"  삼성전자: {price:,}원")
