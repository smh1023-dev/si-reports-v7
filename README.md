# 에스아이 스토리 — 일일 투자 리포트 자동 생성 시스템

매일 아침 7시(한국시간)에 자동으로 미국·한국 주식 시장을 분석해 3개 리포트를 생성합니다.

## ✨ v7 핵심 변경: 자체 포함(self-contained) 구조

이전 버전에서 `data/sp500_tickers.csv` 파일을 못 찾는 오류가 있었습니다.
**이 버전은 종목 데이터를 코드 안에 직접 내장**해서, data 폴더 없이도 작동합니다.

업로드만 하면 끝. 어떤 폴더 구조 깨짐이 있어도 동작합니다.

## 파일 구조 (10개 파일만)

```
si-reports/
├── .github/
│   └── workflows/
│       └── daily_report.yml      ← GitHub Actions 자동 실행
├── templates/
│   ├── free_template.html        ← 무료 보고서
│   ├── us_premium_template.html  ← 미국 프리미엄
│   └── korea_premium_template.html ← 한국 프리미엄
├── report_generator.py           ← 메인 스크립트
├── tickers_data.py               ← ⚠️ 종목 데이터 (366 US + 165 KR + 11 섹터)
├── damodaran.py                  ← Claude API Damodaran 5파트 분석
├── risk_metrics.py               ← MDD/샤프/베타/알파/VaR
├── backtest.py                   ← 1년 백테스트
├── requirements.txt
└── README.md
```

⚠️ **data 폴더 없음** — 모든 데이터가 `tickers_data.py`에 들어있습니다.

## 결과물

| 보고서 | URL | 내용 |
|---|---|---|
| 무료 (메인) | `/` | 시장 요약 + 하이라이트 (TOP 3 제외) |
| 미국 프리미엄 | `/us_premium_report.html` | TOP 3 + Damodaran 5파트 + 펀더멘털 + 리스크 + 백테스트 |
| 한국 프리미엄 | `/korea_premium_report.html` | TOP 3 + Damodaran 5파트 + 리스크 |

URL: `https://[당신의-username].github.io/[저장소-이름]/`

## Damodaran 5파트 분석 (Claude API)

TOP 10 종목에 대해 매일 다음 5가지를 분석:

1. 🛡️ **해자 (Moat)** — 네트워크/브랜드/전환비용/데이터 중 무엇인지
2. 📈 **성장 동인** — 핵심 사업과 매출 견인 요인
3. ⚠️ **리스크 요인** — 규제/경쟁/기술변화/매크로
4. 🚀 **메가트렌드** — AI/데이터센터/플랫폼/네트워크 관련성
5. 📖 **Damodaran 투자 논리** — 가치/성장/리스크 균형 한 줄

추가 정량 분석:
- 📊 위험 변화 (D/E, 매출/EPS 성장률 체크)
- 💰 밸류에이션 (PEG&lt;1.5, P/FCF&lt;25 체크)
- MDD, 샤프, 베타, 알파, VaR 95%, 종합 리스크 점수

## 점수 / 판정

- 기술 점수 = 추세(32) + 모멘텀(20) + 거래량(15) + 52주위치(15) + 돌파(18)
- 펀더 점수 = ROE + PER + PEG + 매출↑ + EPS↑ + D/E + P/FCF
- 종합 = (기술 + 펀더) / 2
- 판정: 🟢 관심(70+ &amp; 200일선 위) / 🟡 보류(55+) / 🔴 미충족

---

## 업로드 절차 (코딩 몰라도 OK)

### 1) GitHub 가입 & 저장소

1. https://github.com → Sign up
2. `+` → `New repository` → 이름 `si-reports`, **Public** → 생성

### 2) 파일 업로드

1. 저장소 메인 → `uploading an existing file` 링크
2. zip 풀어서 나온 모든 파일과 폴더 드래그
   - `.github/`, `templates/` 폴더 통째
   - `.py` `.txt` `.md` 파일들
3. `Commit changes`

**업로드 확인**: 저장소 메인에서 `report_generator.py`, `tickers_data.py`, `templates/` 폴더가 보이면 OK.
data 폴더는 없어도 됩니다.

### 3) GitHub Pages 활성화

`Settings` → 좌측 `Pages` → **Source: `GitHub Actions`** 선택

### 4) Actions 권한

`Settings` → `Actions` → `General` → **Read and write permissions** → Save

### 5) Claude API 키 등록 (Damodaran 분석용, 선택)

**A. Anthropic 가입 + 충전 (5분)**
1. https://console.anthropic.com → Sign up
2. 좌측 `Plans & Billing` → 결제 정보 등록
3. **선불 충전** $5~10 (Add credit)

비용: 매일 약 $0.10, 월 약 $3 (캐시 적용). 선불이라 초과 청구 불가능.

**B. API 키 발급 (1분)**
1. 좌측 `API Keys` → `Create Key`
2. 표시되는 키 복사 (`sk-ant-api03-...`)

**C. GitHub Secrets 등록 (2분)**
1. GitHub 저장소 → `Settings`
2. 좌측 `Secrets and variables` → `Actions`
3. `New repository secret`
4. **Name**: `ANTHROPIC_API_KEY`
5. **Secret**: 위 키 붙여넣기
6. `Add secret`

### 6) 첫 빌드 실행

1. 상단 `Actions` → `Daily SI Investment Report` → `Run workflow`
2. 20~40분 대기
3. 완료 후 `deploy`에 URL 표시

이후 매일 새벽 7시(KST) 자동 실행.

---

## API 키 없을 때

정상 동작합니다. ①②③④⑤ 항목이 산업 평균 문구로 채워집니다. 진짜 종목별 분석을 원하면 위 5단계에서 API 키 등록하세요.

## 캐시

- Damodaran 스토리: 3일 (같은 종목 재호출 방지)
- 백테스트: 7일 (월요일에만 신규 계산)
- GitHub Actions `actions/cache@v4`로 빌드 간 보존

## 자주 묻는 질문

**Q. "data/sp500_tickers.csv 파일을 찾을 수 없습니다" 오류**
A. **v7부터 해결됨**. 종목 데이터가 `tickers_data.py`에 들어가 있어 data 폴더 자체가 불필요합니다. 이 오류가 다시 나오면 `tickers_data.py` 파일이 업로드 안 된 것입니다.

**Q. 빌드 실패**
A. `Actions` 탭에서 실패 단계 확인. 가장 흔한 원인은 yfinance 일시 차단(다음날 자동 복구).

**Q. 한국 펀더멘털 왜 없나요?**
A. yfinance가 한국 종목 재무지표를 거의 제공하지 않습니다. 기술 + 리스크 + Claude 스토리만 분석.

**Q. 비용 폭주 걱정**
A. Anthropic은 선불 충전 시스템. 충전한 만큼만 쓰고 자동 정지.

---

## 면책

- 데이터: Yahoo Finance · LLM: Claude Haiku 4.5
- **투자 참고용**. 매수·매도 권유 아님.
- 백테스트는 과거 성과 기반, 거래비용·세금 미반영.
- 투자 판단과 손익 책임은 전적으로 투자자 본인.
