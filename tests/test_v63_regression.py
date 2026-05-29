"""
v6.3 Regression Guard Tests
===========================
LLM 매매 신호 의존 제거(v6.3) 변경이 merge 충돌이나 revert로
되돌려지는 것을 막기 위한 정적 분석(AST) 기반 회귀 테스트.

대상 파일:
- btc/btc_trading_agent.py
- stocks/stock_trading_agent.py

규칙:
- analyze_with_ai() 함수 정의는 DEPRECATED 보존 상태여야 함
- 하지만 어떤 코드도 analyze_with_ai()를 호출해서는 안 됨
- get_trading_signal() 함수 body 내부에서도 analyze_with_ai 호출 0건
"""

import ast
import unittest
from pathlib import Path

# 프로젝트 루트: tests/ 의 부모
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BTC_AGENT = PROJECT_ROOT / "btc" / "btc_trading_agent.py"
KR_AGENT = PROJECT_ROOT / "stocks" / "stock_trading_agent.py"


def _parse_file(path: Path) -> ast.Module:
    """파일을 AST로 파싱한다. 실패 시 명확한 메시지를 포함한 AssertionError."""
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AssertionError(f"소스 파일을 찾을 수 없음: {path}")
    except OSError as e:
        raise AssertionError(f"소스 파일 읽기 실패: {path} — {e}")

    try:
        return ast.parse(source)
    except SyntaxError as e:
        raise AssertionError(f"AST 파싱 실패 ({path.name}): {e}")


class CallFinder(ast.NodeVisitor):
    """AST 트리에서 특정 함수 호출(ast.Call)을 수집하는 비지터."""

    def __init__(self, target_name: str) -> None:
        self.target = target_name
        self.calls: list[int] = []  # 발견된 라인 번호 목록

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if isinstance(node.func, ast.Name) and node.func.id == self.target:
            self.calls.append(node.lineno)
        # Attribute 호출(self.analyze_with_ai() 등)도 검사
        elif isinstance(node.func, ast.Attribute) and node.func.attr == self.target:
            self.calls.append(node.lineno)
        self.generic_visit(node)


class TestV63RegressionGuard(unittest.TestCase):
    """v6.3 — LLM 매매 신호 제거 regression 가드."""

    # ─────────────────────────────────────────────────────────────
    # TC-1: BTC 메인 루프에서 analyze_with_ai() 호출 0건
    # ─────────────────────────────────────────────────────────────
    def test_btc_main_loop_does_not_call_analyze_with_ai(self) -> None:
        """btc_trading_agent.py 전체에서 analyze_with_ai() 호출이 없어야 한다.

        함수 정의(def analyze_with_ai)는 DEPRECATED 보존이므로 허용.
        호출(analyze_with_ai(...)) 만 금지.
        """
        tree = _parse_file(BTC_AGENT)
        finder = CallFinder("analyze_with_ai")
        finder.visit(tree)
        self.assertEqual(
            finder.calls,
            [],
            f"[v6.3 regression] btc_trading_agent.py에서 analyze_with_ai() 호출 발견 "
            f"— 라인: {finder.calls}. "
            f"v6.3 룰 기반 신호(rule_based_btc_signal)로 되돌아가야 함.",
        )

    # ─────────────────────────────────────────────────────────────
    # TC-2: KR get_trading_signal() 함수 body에서 analyze_with_ai() 호출 0건
    # ─────────────────────────────────────────────────────────────
    def test_kr_get_trading_signal_does_not_call_analyze_with_ai(self) -> None:
        """stock_trading_agent.py의 get_trading_signal() 내부에서
        analyze_with_ai() 호출이 없어야 한다.

        v6.3 변경: 회색지대 AI 호출 제거 → RULE_PRIMARY / RULE_DEFAULT 단일 경로.
        """
        tree = _parse_file(KR_AGENT)

        found_function = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "get_trading_signal":
                found_function = True
                finder = CallFinder("analyze_with_ai")
                finder.visit(node)  # 이 함수 body만 탐색
                self.assertEqual(
                    finder.calls,
                    [],
                    f"[v6.3 regression] stock_trading_agent.py get_trading_signal()에서 "
                    f"analyze_with_ai() 호출 발견 — 라인: {finder.calls}. "
                    f"v6.3 룰 기반 단일 경로(RULE_PRIMARY/RULE_DEFAULT)로 되돌아가야 함.",
                )

        self.assertTrue(
            found_function,
            "stock_trading_agent.py에서 get_trading_signal 함수를 찾을 수 없음 — "
            "함수가 삭제되거나 이름이 바뀌었는지 확인.",
        )

    # ─────────────────────────────────────────────────────────────
    # TC-3: BTC analyze_with_ai 정의 주변에 DEPRECATED 주석 보존 확인
    # ─────────────────────────────────────────────────────────────
    def test_btc_analyze_with_ai_fully_removed(self) -> None:
        """PR #27: btc_trading_agent.py 의 analyze_with_ai/_call_btc_haiku 완전 삭제 확인.

        Phase 4 페이퍼 검증 완료. 함수가 다시 추가되면 fail (재도입 방지).
        대체: rule_based_btc_signal (v6.3).
        """
        source = BTC_AGENT.read_text(encoding="utf-8")
        self.assertNotIn(
            "def analyze_with_ai",
            source,
            "btc_trading_agent.py 에 analyze_with_ai 가 재도입됨 — "
            "v6.3 룰 기반 정책 위배 (rule_based_btc_signal 사용 필수).",
        )
        self.assertNotIn(
            "def _call_btc_haiku",
            source,
            "btc_trading_agent.py 에 _call_btc_haiku 가 재도입됨 — "
            "매매 결정 루프 LLM 의존 금지 (CLAUDE.md 정책).",
        )

    # ─────────────────────────────────────────────────────────────
    # TC-4: KR analyze_with_ai 정의 주변에 DEPRECATED 주석 보존 확인
    # ─────────────────────────────────────────────────────────────
    def test_kr_deprecation_comment_preserved(self) -> None:
        """stock_trading_agent.py의 def analyze_with_ai 정의 앞뒤 10줄 내에
        'DEPRECATED' 문자열이 있어야 한다.
        """
        lines = KR_AGENT.read_text(encoding="utf-8").splitlines()
        definition_lines = [i for i, ln in enumerate(lines) if "def analyze_with_ai" in ln]

        self.assertTrue(
            len(definition_lines) >= 1,
            f"stock_trading_agent.py에서 'def analyze_with_ai' 정의를 찾지 못함 — "
            f"DEPRECATED 함수가 의도치 않게 삭제되었는지 확인. ({KR_AGENT})",
        )

        deprecated_found = False
        for def_lineno in definition_lines:
            start = max(0, def_lineno - 10)
            end = min(len(lines), def_lineno + 11)
            window = "\n".join(lines[start:end])
            if "DEPRECATED" in window:
                deprecated_found = True
                break

        self.assertTrue(
            deprecated_found,
            f"[v6.3 regression] stock_trading_agent.py analyze_with_ai 정의 앞뒤 10줄 내에 "
            f"'DEPRECATED' 주석이 없음. Phase 4 완료 전 호출 금지 의도가 명시되어야 함.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
