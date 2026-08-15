# OpenClaw 고급 Google Sheets 대시보드 시스템

## 🎯 개요

OpenClaw 고급 Google Sheets 대시보드는 자동매매 시스템의 모든 거래 데이터를 실시간으로 기록하고, 포트폴리오를 분석하며, 위험을 관리하는 완벽한 금융 대시보드 솔루션입니다.

### ✨ 주요 기능

- **📊 실시간 거래 기록**: 모든 매수/매도/손절/익절 거래 자동 기록
- **💼 포트폴리오 요약**: 현재 자산 현황 및 평가손익 실시간 계산
- **📈 통계 분석**: 일간/주간/월간 거래 성과 분석
- **⚠️ 위험 관리**: MDD, 손익비, 샤프지표 등 전문가급 위험 지표
- **🔔 스마트 알림**: 손실 경고, 수익 목표 달성, 시스템 상태 알림
- **📊 자동 차트**: 수익률 추이, 거래량 분석 등 시각화된 데이터
- **🤖 완전 자동화**: 크론 기반 10분 단위 자동 업데이트

---

## 🏗️ 시스템 아키텍처

### 📁 파일 구조

```
workspace/
├── common/
│   ├── sheets_logger.py          # Google Sheets 자동 기록 모듈
│   ├── sheets_manager.py         # 고급 대시보드 관리자
│   └── alert_system.py           # 알림 시스템
├── agents/
│   └── daily_loss_analyzer.py    # 일일 손실 분석기
├── scripts/
│   ├── dashboard_runner.py       # 통합 대시보드 실행기
│   └── setup_dashboard_cron.sh   # 크론 설정 스크립트
└── docs/
    └── GOOGLE_SHEETS_DASHBOARD.md # 이 문서
```

### 🔗 Google Sheets 구조

| 시트 | ID | 목적 |
|------|----|-----|
| **메인 거래기록** | `1HXBiwg38i2LrgOgC3mjokH0sTk7qgq7Q8o4jdWOe58s` | 모든 거래 상세 기록 |
| **포트폴리오 요약** | `<GOOGLE_SHEET_ID>` | 실시간 자산 현황 |
| **통계 분석** | `16ai_PTJ6XfIpPaio-AnaNY7aQaDPrdqtrvpA91nUH14` | 거래 통계 및 성과 |
| **위험 관리** | `1MijDcgoFp6hY1bhl9fhHKTBFpK4yBXZL9lzNZ_MaK-w` | 위험 지표 및 관리 |

---

## 🚀 설치 및 설정

### 1. 필수 조건

- **Python 3.11+** 및 가상환경
- **gog CLI** (Google OAuth 인증)
- **Google Sheets API** 활성화
- **Supabase** 데이터베이스
- **Telegram Bot** (알림용)

### 2. 환경변수 설정

```bash
# Google Sheets 설정
export GOOGLE_SHEET_ID="1HXBiwg38i2LrgOgC3mjokH0sTk7qgq7Q8o4jdWOe58s"
export GOOGLE_SHEET_TAB="시트1"
export USE_GOG="true"
export GOG_KEYRING_PASSWORD="<GOG_KEYRING_PASSWORD>"

# 알림 설정
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"

# 선택적 기능
export BRAVE_API_KEY="your_brave_api_key"  # 뉴스 검색용
```

### 3. gog CLI 인증

```bash
# gog CLI 설치 (Docker에서 복사)
cp /var/lib/docker/rootfs/overlayfs/*/usr/local/bin/gog ./gog-docker
chmod +x ./gog-docker

# OAuth 인증
export GOG_KEYRING_PASSWORD="<GOG_KEYRING_PASSWORD>"
./gog-docker auth add your-email@gmail.com --services sheets
```

### 4. 의존성 설치

```bash
source .venv/bin/activate
pip install gspread google-auth google-auth-oauthlib google-auth-httplib2
```

---

## 📊 대시보드 기능 상세

### 🏷️ 메인 거래기록 시트

**헤더 구조:**
```
거래일시 | 시장 | 매매구분 | 종목코드 | 종목명 | 가격 | 수량 | 수익률 | 진입근거 | 뉴스요약 | 에이전트
```

**기능:**
- 모든 거래의 상세 정보 자동 기록
- 시장별 구분 (BTC/KR/US)
- 매매구분 명확히 표시 (매수/매도/손절/익절)
- 종목명 자동 완성
- 가격 포맷팅 (BTC: 원, KR: 원, US: 달러)
- 수익률 자동 계산 및 표시

### 💼 포트폴리오 요약 시트

**헤더 구조:**
```
항목 | BTC | KR주식 | US주식 | 합계 | 업데이트시간
```

**기능:**
- **현재가치**: 시장별 현재 포지션 가치
- **총 평가손익**: 실현 + 미실현 손익
- **수익률**: 투자 대비 수익률 (%)
- **보유수량**: 시장별 보유 수량
- **평균단가**: 평균 진입 가격
- **오늘손익**: 당일 발생 손익

### 📈 통계 분석 시트

**헤더 구조:**
```
기간 | 총거래횟수 | 수익거래 | 손실거래 | 승률(%) | 평균수익률(%) | 최대손실률(%)
```

**기능:**
- **일간 통계**: 당일 거래 성과
- **주간 통계**: 최근 7일 거래 성과
- **월간 통계**: 최근 30일 거래 성과
- **승률 계산**: 수익 거래 비율
- **위험 지표**: 최대 손실률 추적

### ⚠️ 위험 관리 시트

**헤더 구조:**
```
지표 | BTC | KR주식 | US주식 | 전체합계 | 위험등급
```

**기능:**
- **최대손실률 (MDD)**: 최대 낙폭 추적
- **손익비**: 수익/손실 평균 비율
- **샤프지표**: 위험 조정 수익률
- **최대포지션크기**: 과도한 포지션 모니터링
- **위험등급**: 자동 위험 등급 분류

---

## 🔔 알림 시스템

### 알림 종류

| 알림 유형 | 조건 | 심각도 | 메시지 예시 |
|-----------|------|--------|------------|
| **위험 손실** | 손실률 ≤ -10% | 위험 | 🚨 위험 손실: BTC -10.5% 손실 |
| **손실 경고** | 손실률 ≤ -5% | 경고 | ⚠️ 손실 경고: AAPL -5.2% 손실 |
| **수익 목표** | 수익률 ≥ 10% | 정보 | 🎯 목표 달성: BTC +12.3% 수익 |
| **포지션 과다** | 포지션 ≥ 1억원 | 경고 | 📊 포지션 과다: 삼성전자 1.2억원 |
| **시스템 오류** | API 연결 실패 | 위험 | 🔴 Supabase 연결 실패 |

### 일일 요약 알림

매일 09:00에 전송되는 일일 거래 요약:
- 총 거래 횟수
- 수익/손실 거래 수
- 승률
- 총 손익

---

## 🤖 자동화 시스템

### 크론 작업

```bash
# 10분마다 대시보드 업데이트
*/10 * * * * cd /home/wlsdud5035/.openclaw/workspace && .venv/bin/python scripts/dashboard_runner.py

# 매일 자정 일일 손실 분석
0 0 * * * cd /home/wlsdud5035/.openclaw/workspace && .venv/bin/python agents/daily_loss_analyzer.py

# 매일 09:00 알림 시스템 실행
0 9 * * * cd /home/wlsdud5035/.openclaw/workspace && .venv/bin/python common/alert_system.py
```

### 실행 방법

#### 수동 실행

```bash
# 전체 대시보드 업데이트
.venv/bin/python scripts/dashboard_runner.py

# 개별 기능 실행
.venv/bin/python common/sheets_manager.py      # 포트폴리오/통계/위험 관리
.venv/bin/python common/alert_system.py        # 알림 시스템
.venv/bin/python agents/daily_loss_analyzer.py # 일일 손실 분석
```

#### 크론 설정

```bash
# 자동 크론 설정 스크립트 실행
./scripts/setup_dashboard_cron.sh
```

---

## 📊 모듈 상세 설명

### common/sheets_logger.py

**역할**: Google Sheets에 거래 데이터 자동 기록

**주요 함수:**
- `append_trade()`: 거래 데이터 시트에 추가
- `is_configured()`: 설정 상태 확인
- `_append_via_gog()`: gog CLI로 데이터 전송

**특징:**
- gog CLI 및 gspread 이중 지원
- 자동 가격 포맷팅
- 종목명 자동 완성
- 안전한 에러 처리

### common/sheets_manager.py

**역할**: 고급 대시보드 관리 및 데이터 분석

**주요 클래스:**
- `AdvancedSheetsManager`: 전체 대시보드 관리

**주요 기능:**
- 포트폴리오 요약 자동 계산
- 통계 분석 (일간/주간/월간)
- 위험 관리 지표 계산
- 실시간 데이터 업데이트

### common/alert_system.py

**역할**: 스마트 알림 시스템

**주요 클래스:**
- `AlertSystem`: 알림 관리

**알림 조건:**
- 손실률 기반 경고
- 수익 목표 달성 알림
- 포지션 크기 모니터링
- 시스템 상태 점검

### scripts/dashboard_runner.py

**역할**: 통합 대시보드 실행기

**기능:**
- 모든 모듈 통합 실행
- 성공률 측정 및 보고
- 실행 시간 측정
- 링크 정보 제공

---

## 🔧 문제 해결

### 일반적인 문제

1. **gog CLI 인증 실패**
   ```bash
   # 재인증 실행
   ./gog-docker auth add your-email@gmail.com --services sheets --force-consent
   ```

2. **Google Sheets API 오류**
   - Google Cloud Console에서 Sheets API 활성화 확인
   - 서비스 계정 권한 확인

3. **데이터 타입 오류**
   - `_safe_float()` 함수로 자동 처리됨
   - None 값은 0.0으로 변환

4. **텔레그램 알림 실패**
   - Bot Token과 Chat ID 확인
   - 봇에게 메시지 전송 권한 확인

### 로그 확인

```bash
# 대시보드 로그
tail -f /var/log/openclaw_dashboard.log

# 알림 시스템 로그
tail -f /var/log/openclaw_alerts.log

# 일일 분석 로그
tail -f /var/log/openclaw_analyzer.log
```

---

## 📈 성능 최적화

### 실행 시간

- **전체 대시보드 업데이트**: 약 10-15초
- **개별 모듈 실행**: 약 3-5초
- **데이터 조회**: Supabase 기반 최적화

### 리소스 사용

- **메모리**: 최소 512MB 권장
- **CPU**: 싱글 코어로 충분
- **네트워크**: 안정적인 인터넷 연결 필요

### 최적화 팁

1. **쿼리 최적화**: 필요한 필드만 선택
2. **캐싱**: 반복 데이터는 캐싱 활용
3. **배치 처리**: 대량 데이터는 배치로 처리
4. **타임아웃 설정**: 적절한 타임아웃 값 설정

---

## 🔮 향후 개선 계획

### 단기 개선 (1-2주)

- [ ] 실시간 차트 생성 기능
- [ ] 더 많은 위험 지표 추가
- [ ] 이메일 알림 기능
- [ ] 모바일 최적화 뷰

### 중기 개선 (1-2개월)

- [ ] 머신러닝 기반 예측
- [ ] 포트폴리오 최적화 제안
- [ ] 세금 자동 계산
- [ ] 다중 통화 지원

### 장기 개선 (3-6개월)

- [ ] AI 기반 투자 조언
- [ ] 소셜 트레이딩 기능
- [ ] 백테스팅 통합
- [ ] API 제공 (REST/GraphQL)

---

## 📞 지원 및 연락처

### 기술 지원

- **문서**: 이 문서의 문제 해결 섹션 참조
- **로그**: `/var/log/openclaw_*.log` 파일 확인
- **테스트**: `scripts/dashboard_runner.py` 실행으로 상태 확인

### 기여 방법

1. 버그 리포트: GitHub Issues
2. 기능 요청: GitHub Discussions
3. 코드 기여: Pull Request
4. 문서 개선: 직접 수정 후 PR

---

## 📄 라이선스

이 프로젝트는 MIT 라이선스 하에 배포됩니다. 자유롭게 사용, 수정, 배포할 수 있습니다.

---

**🎉 OpenClaw 고급 Google Sheets 대시보드로 완벽한 자동매매 관리를 경험하세요!**

*마지막 업데이트: 2026년 3월 1일*
