#!/usr/bin/env python3
"""
SI Investment Report — Daily Generator (v3)

3개 보고서를 생성합니다:
  1. si_investment_report.html   (무료, TOP 3 제외)
  2. us_premium_report.html      (미국 프리미엄, TOP 3 포함)
  3. korea_premium_report.html   (한국 프리미엄, TOP 3 포함)

데이터 소스:
  - 가격/거래량: yfinance
  - 미국 펀더멘털: yfinance .info (가능한 종목만)
  - 한국 종목 리스트: data/krx_tickers.csv
  - 미국 종목 리스트: Wikipedia 실시간 + CSV fallback
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np
import yfinance as yf

warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)


# ============================================================
#  설정
# ============================================================

# 시장 인덱스 / 환율 (시장 요약 카드용)
MARKET_INDEX_TICKERS = [
    # 미국 지수
    ("^GSPC",   "S&P 500",    "지수", "us_index"),
    ("^IXIC",   "나스닥",      "지수", "us_index"),
    ("^DJI",    "다우존스",    "지수", "us_index"),
    ("^RUT",    "러셀 2000",   "지수", "us_index"),
    # 한국 지수
    ("^KS11",   "코스피",      "지수", "kr_index"),
    ("^KQ11",   "코스닥",      "지수", "kr_index"),
    # 변동성
    ("^VIX",    "VIX",        "변동성", "macro"),
    # 미국 ETF
    ("SPY",     "SPY",        "ETF", "etf"),
    ("QQQ",     "QQQ",        "ETF", "etf"),
    ("DIA",     "DIA",        "ETF", "etf"),
    ("IWM",     "IWM",        "ETF", "etf"),
    # 매크로
    ("TLT",     "TLT 장기채",  "채권", "macro"),
    ("GLD",     "금(GLD)",    "원자재", "macro"),
    ("KRW=X",   "USD/KRW",    "환율", "macro"),
]

MAX_WORKERS = 8
FUNDAMENTAL_WORKERS = 6   # info 호출은 더 느리고 차단도 잘 됨
DATA_PERIOD = "1y"


# ============================================================
#  종목 풀 로드
# ============================================================

def load_us_universe() -> pd.DataFrame:
    """미국 종목 풀: 2단계 fallback.
    1) Wikipedia에서 실시간 S&P 500 (성공시 ~500개)
    2) tickers_data.US_TICKERS (366개, 자체 포함)
    """
    log.info("미국 종목 리스트 로딩...")
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        tables = pd.read_html(url, header=0)
        df = tables[0]
        df = df.rename(columns={
            'Symbol': 'ticker',
            'Security': 'name',
            'GICS Sector': 'sector',
        })
        df['ticker'] = df['ticker'].str.replace('.', '-', regex=False)
        df = df[['ticker', 'name', 'sector']].drop_duplicates(subset=['ticker'])
        log.info("  ✓ Wikipedia에서 %d개 종목 로드", len(df))
        return df.reset_index(drop=True)
    except Exception as e:
        log.warning("  Wikipedia 실패 (%s) - 내장 리스트 사용", e)

    from tickers_data import US_TICKERS
    df = pd.DataFrame(US_TICKERS, columns=['ticker', 'name', 'sector'])
    df = df.drop_duplicates(subset=['ticker']).reset_index(drop=True)
    log.info("  ✓ 내장 리스트에서 %d개 종목 로드", len(df))
    return df


def load_krx_universe() -> pd.DataFrame:
    """한국 종목 풀: tickers_data.KR_TICKERS (165개, 자체 포함)."""
    log.info("한국 종목 리스트 로딩...")
    from tickers_data import KR_TICKERS
    df = pd.DataFrame(KR_TICKERS, columns=['ticker', 'name', 'sector'])
    df = df.drop_duplicates(subset=['ticker']).reset_index(drop=True)
    log.info("  ✓ 내장 리스트에서 %d개 종목 로드", len(df))
    return df



# ============================================================
#  가격 데이터 수집 (모든 종목)
# ============================================================

def fetch_price_data(ticker: str) -> dict | None:
    """단일 티커의 가격/추세 지표 계산."""
    try:
        df = yf.Ticker(ticker).history(period=DATA_PERIOD, auto_adjust=True)
        if len(df) < 50:
            return None

        df = df.sort_index()
        close = df['Close'].values
        volume = df['Volume'].values

        if len(close) < 2 or close[-1] <= 0:
            return None

        sma50 = pd.Series(close).rolling(50).mean().values
        sma200 = pd.Series(close).rolling(200).mean().values if len(close) >= 200 else None

        change_pct = (close[-1] / close[-2] - 1) * 100

        vol_today = int(volume[-1]) if not np.isnan(volume[-1]) else 0
        avg_vol_20 = int(np.nanmean(volume[-20:])) if len(volume) >= 20 else vol_today
        vol_ratio = vol_today / avg_vol_20 if avg_vol_20 > 0 else 1.0

        above_200 = bool(sma200 is not None and not np.isnan(sma200[-1]) and close[-1] > sma200[-1])
        sma200_break = False
        if sma200 is not None and len(sma200) >= 2 and not np.isnan(sma200[-2]):
            sma200_break = bool(close[-2] <= sma200[-2] and close[-1] > sma200[-1])

        above_50 = bool(not np.isnan(sma50[-1]) and close[-1] > sma50[-1])
        sma50_break = False
        if len(sma50) >= 2 and not np.isnan(sma50[-2]):
            sma50_break = bool(close[-2] <= sma50[-2] and close[-1] > sma50[-1])

        # 골든크로스 (50일선이 200일선을 상향 돌파)
        golden_cross = False
        if sma200 is not None and len(sma50) >= 2 and len(sma200) >= 2:
            if not np.isnan(sma50[-1]) and not np.isnan(sma50[-2]) \
               and not np.isnan(sma200[-1]) and not np.isnan(sma200[-2]):
                golden_cross = bool(sma50[-2] <= sma200[-2] and sma50[-1] > sma200[-1])

        recent = close[-min(252, len(close)):]
        high_52w = float(np.max(recent))
        low_52w = float(np.min(recent))
        pct_from_high = (close[-1] / high_52w - 1) * 100

        # 변동성
        if len(close) >= 21:
            returns = np.diff(close[-21:]) / close[-21:-1]
            volatility = float(np.std(returns) * np.sqrt(252) * 100)
        else:
            volatility = 0.0

        # 추세 지속 (3개월 수익률)
        if len(close) >= 63:
            ret_3m = (close[-1] / close[-63] - 1) * 100
        else:
            ret_3m = 0.0
        # 6개월 수익률
        if len(close) >= 126:
            ret_6m = (close[-1] / close[-126] - 1) * 100
        else:
            ret_6m = ret_3m

        return {
            'ticker': ticker,
            'price': float(close[-1]),
            'change_pct': float(change_pct),
            'volume': vol_today,
            'avg_volume_20d': avg_vol_20,
            'volume_ratio': float(vol_ratio),
            'above_sma50': above_50,
            'above_sma200': above_200,
            'sma50_break': sma50_break,
            'sma200_break': sma200_break,
            'golden_cross': golden_cross,
            'high_52w': high_52w,
            'low_52w': low_52w,
            'pct_from_52w_high': float(pct_from_high),
            'volatility_20d': volatility,
            'return_3m': float(ret_3m),
            'return_6m': float(ret_6m),
            # 백테스트·리스크용 시계열 (직렬화 안 되므로 dataframe에는 빼고 별도 보관)
            '_close_series': pd.Series(close, index=df.index),
        }
    except Exception:
        return None


def fetch_universe_prices(universe: pd.DataFrame, label: str) -> tuple[pd.DataFrame, dict]:
    """가격 데이터 병렬 수집.

    Returns:
        (DataFrame: 요약 지표, dict: {ticker: close_series})
    """
    log.info("[%s] %d개 종목 가격 데이터 수집...", label, len(universe))
    start = time.time()
    results = []
    price_series_map = {}
    failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_price_data, t): t for t in universe['ticker']}
        for i, future in enumerate(as_completed(futures), 1):
            data = future.result()
            if data is not None:
                # 시리즈 분리
                series = data.pop('_close_series', None)
                if series is not None:
                    price_series_map[data['ticker']] = series
                results.append(data)
            else:
                failed += 1
            if i % 100 == 0:
                log.info("  [%s] 진행: %d/%d", label, i, len(futures))

    elapsed = time.time() - start
    log.info("[%s] 가격: %d/%d 성공, %.1f초", label, len(results), len(universe), elapsed)

    if not results:
        return pd.DataFrame(), {}

    df = pd.DataFrame(results)
    df = df.merge(universe[['ticker', 'name', 'sector']], on='ticker', how='left')
    return df, price_series_map


# ============================================================
#  펀더멘털 수집 (미국 종목만, 시간 제한)
# ============================================================

def fetch_fundamentals(ticker: str) -> dict:
    """yfinance .info에서 펀더멘털 지표 추출.

    한국 종목이나 데이터 없는 종목은 모두 None 반환.
    """
    out = {
        'ticker': ticker,
        'market_cap': None,
        'pe': None,
        'peg': None,
        'pb': None,
        'p_fcf': None,
        'roe': None,
        'debt_equity': None,
        'rev_growth': None,
        'eps_growth': None,
        'dividend_yield': None,
        'industry': None,
        'has_fundamentals': False,
    }
    try:
        info = yf.Ticker(ticker).info
        if not info:
            return out

        # yfinance가 주는 키들
        out['market_cap'] = info.get('marketCap')
        out['pe'] = info.get('trailingPE') or info.get('forwardPE')
        out['peg'] = info.get('pegRatio') or info.get('trailingPegRatio')
        out['pb'] = info.get('priceToBook')
        out['roe'] = info.get('returnOnEquity')
        out['debt_equity'] = info.get('debtToEquity')
        out['rev_growth'] = info.get('revenueGrowth')
        out['eps_growth'] = info.get('earningsGrowth') or info.get('earningsQuarterlyGrowth')
        out['dividend_yield'] = info.get('dividendYield')
        out['industry'] = info.get('industry')

        # P/FCF (시총 / 잉여현금흐름)
        mcap = info.get('marketCap')
        fcf = info.get('freeCashflow')
        if mcap and fcf and fcf > 0:
            out['p_fcf'] = mcap / fcf

        # roe, growth는 % 변환 (yfinance는 0.15 = 15% 형태)
        if out['roe'] is not None and isinstance(out['roe'], (int, float)):
            out['roe'] = out['roe'] * 100
        if out['rev_growth'] is not None and isinstance(out['rev_growth'], (int, float)):
            out['rev_growth'] = out['rev_growth'] * 100
        if out['eps_growth'] is not None and isinstance(out['eps_growth'], (int, float)):
            out['eps_growth'] = out['eps_growth'] * 100
        if out['dividend_yield'] is not None and isinstance(out['dividend_yield'], (int, float)):
            # 신버전 yfinance는 % 단위(2.5) - 구버전은 비율(0.025)
            if out['dividend_yield'] < 1:
                out['dividend_yield'] = out['dividend_yield'] * 100

        # 데이터가 일부라도 있으면 True
        out['has_fundamentals'] = any([
            out['pe'], out['peg'], out['roe'], out['rev_growth'], out['market_cap']
        ])

    except Exception:
        pass

    return out


def fetch_universe_fundamentals(tickers: list[str], label: str, limit: int = 0) -> pd.DataFrame:
    """펀더멘털 병렬 수집 (시간 제한)."""
    if limit:
        tickers = tickers[:limit]
    log.info("[%s] %d개 종목 펀더멘털 수집...", label, len(tickers))
    start = time.time()
    results = []
    success = 0

    with ThreadPoolExecutor(max_workers=FUNDAMENTAL_WORKERS) as ex:
        futures = {ex.submit(fetch_fundamentals, t): t for t in tickers}
        for i, future in enumerate(as_completed(futures), 1):
            data = future.result()
            results.append(data)
            if data['has_fundamentals']:
                success += 1
            if i % 50 == 0:
                log.info("  [%s] 진행: %d/%d (펀더멘털 있음 %d)",
                         label, i, len(futures), success)

    elapsed = time.time() - start
    log.info("[%s] 펀더멘털: %d개 중 %d개 채워짐, %.1f초",
             label, len(tickers), success, elapsed)
    return pd.DataFrame(results)


# ============================================================
#  시장 요약
# ============================================================

def fetch_market_summary() -> tuple[dict[str, list[dict]], str]:
    """시장 요약 카드. 그룹별로 묶어 반환."""
    log.info("시장 요약 수집...")
    results = []
    vix = None

    with ThreadPoolExecutor(max_workers=6) as ex:
        future_map = {}
        for ticker, label, kind, group in MARKET_INDEX_TICKERS:
            future = ex.submit(fetch_price_data, ticker)
            future_map[future] = (ticker, label, kind, group)

        for future in as_completed(future_map):
            ticker, label, kind, group = future_map[future]
            data = future.result()
            if data is None:
                continue

            value = data['price']
            change_pct = data['change_pct']

            if ticker == 'KRW=X':
                value_str = f"{value:,.2f}"
            elif ticker in ('^KS11', '^KQ11'):
                value_str = f"{value:,.2f}"
            else:
                value_str = f"{value:,.2f}"

            results.append({
                'ticker': ticker,
                'label': label,
                'kind': kind,
                'group': group,
                'value_str': value_str,
                'change_pct': change_pct,
            })

            if ticker == '^VIX':
                vix = value

    order = {t: i for i, (t, _, _, _) in enumerate(MARKET_INDEX_TICKERS)}
    results.sort(key=lambda x: order.get(x['ticker'], 99))

    grouped: dict[str, list[dict]] = {
        'us_index': [], 'kr_index': [], 'etf': [], 'macro': []
    }
    for r in results:
        grouped[r['group']].append(r)

    interpret = build_market_interpret(vix, results)
    return grouped, interpret


def build_market_interpret(vix: float | None, results: list[dict]) -> str:
    """시장 코멘트."""
    parts = []
    if vix is not None:
        if vix < 15:
            parts.append(f"VIX {vix:.1f} — 변동성 매우 낮음. 위험자산 선호 환경.")
        elif vix < 20:
            parts.append(f"VIX {vix:.1f} — 변동성 안정 구간.")
        elif vix < 30:
            parts.append(f"VIX {vix:.1f} — 변동성 확대 구간. 신중한 진입 권장.")
        else:
            parts.append(f"VIX {vix:.1f} — 변동성 과열. 리스크 관리 우선.")

    sp = next((r for r in results if r['ticker'] == '^GSPC'), None)
    if sp:
        if sp['change_pct'] >= 1:
            parts.append(f"S&P 500 +{sp['change_pct']:.2f}% 상승.")
        elif sp['change_pct'] <= -1:
            parts.append(f"S&P 500 {sp['change_pct']:.2f}% 하락.")

    ks = next((r for r in results if r['ticker'] == '^KS11'), None)
    if ks:
        if ks['change_pct'] >= 1:
            parts.append(f"코스피 +{ks['change_pct']:.2f}% 상승.")
        elif ks['change_pct'] <= -1:
            parts.append(f"코스피 {ks['change_pct']:.2f}% 하락.")

    return ' '.join(parts) if parts else "오늘의 시장 데이터를 확인하세요."


# ============================================================
#  점수 / 판정 / Damodaran 스토리 라벨
# ============================================================

def compute_score(row: dict) -> float:
    """기술적 종합 점수 (0~100)."""
    bd = compute_score_breakdown(row)
    return min(round(bd['total'], 1), 100.0)


def compute_score_breakdown(row: dict) -> dict:
    """기술적 점수 분해 - 영역별 세부 점수.

    반환:
        {
          'trend': 28,         # 추세 (만점 40)
          'momentum': 12,      # 모멘텀 (만점 20)
          'volume': 5,         # 거래량 (만점 15)
          'position_52w': 10,  # 52주 위치 (만점 15)
          'breakout': 6,       # 돌파 (만점 10) - 위 4개와 별도 가산
          'total': 61,
        }
    """
    # 추세 (32) — 단순 위치 + 돌파는 별도
    trend = 0.0
    if row.get('above_sma50'):
        trend += 12
    if row.get('above_sma200'):
        trend += 20

    # 돌파 (8) — 라이브 신호
    breakout = 0.0
    if row.get('sma50_break'):
        breakout += 4
    if row.get('sma200_break'):
        breakout += 6
    if row.get('golden_cross'):
        breakout += 8
    breakout = min(breakout, 18)  # 한 종목당 최대 18

    # 모멘텀 (20)
    momentum = 0.0
    change_pct = row.get('change_pct') or 0
    momentum += min(max(change_pct, 0), 5) / 5 * 10
    ret_3m = row.get('return_3m') or 0
    momentum += min(max(ret_3m, 0), 30) / 30 * 10

    # 거래량 (15)
    vol = 0.0
    vr = row.get('volume_ratio') or 1
    if vr >= 3:
        vol = 15
    elif vr >= 2:
        vol = 10
    elif vr >= 1.5:
        vol = 5

    # 52주 위치 (15)
    pos = 0.0
    dist = abs(row.get('pct_from_52w_high') or -100)
    if dist <= 2:
        pos = 15
    elif dist <= 5:
        pos = 10
    elif dist <= 10:
        pos = 5

    total = trend + breakout + momentum + vol + pos
    return {
        'trend': round(trend, 1),
        'momentum': round(momentum, 1),
        'volume': round(vol, 1),
        'position_52w': round(pos, 1),
        'breakout': round(breakout, 1),
        'total': round(total, 1),
    }


def compute_fundamental_score(row: dict) -> float | None:
    """펀더멘털 점수 (0~100). 데이터 없으면 None."""
    if not row.get('has_fundamentals'):
        return None

    score = 0.0
    weights_used = 0

    # ROE (20점)
    roe = row.get('roe')
    if roe is not None:
        weights_used += 20
        if roe >= 25:
            score += 20
        elif roe >= 15:
            score += 15
        elif roe >= 10:
            score += 10
        elif roe >= 5:
            score += 5

    # PER (15점, 낮을수록 + 단 음수 제외)
    pe = row.get('pe')
    if pe is not None and pe > 0:
        weights_used += 15
        if pe <= 10:
            score += 15
        elif pe <= 15:
            score += 12
        elif pe <= 20:
            score += 8
        elif pe <= 30:
            score += 4

    # PEG (15점)
    peg = row.get('peg')
    if peg is not None and peg > 0:
        weights_used += 15
        if peg <= 1:
            score += 15
        elif peg <= 1.5:
            score += 10
        elif peg <= 2:
            score += 5

    # 매출 성장률 (15점)
    rg = row.get('rev_growth')
    if rg is not None:
        weights_used += 15
        if rg >= 20:
            score += 15
        elif rg >= 10:
            score += 10
        elif rg >= 5:
            score += 5

    # EPS 성장률 (15점)
    eg = row.get('eps_growth')
    if eg is not None:
        weights_used += 15
        if eg >= 20:
            score += 15
        elif eg >= 10:
            score += 10
        elif eg >= 5:
            score += 5

    # Debt/Equity (10점, 낮을수록)
    de = row.get('debt_equity')
    if de is not None and de >= 0:
        weights_used += 10
        if de <= 30:
            score += 10
        elif de <= 50:
            score += 7
        elif de <= 100:
            score += 4

    # P/FCF (10점)
    pfcf = row.get('p_fcf')
    if pfcf is not None and pfcf > 0:
        weights_used += 10
        if pfcf <= 15:
            score += 10
        elif pfcf <= 25:
            score += 6
        elif pfcf <= 40:
            score += 3

    if weights_used == 0:
        return None

    # 가중치 정규화 (실제 사용 가중치 대비 100점으로 환산)
    return round(score / weights_used * 100, 1)


def compute_verdict(tech_score: float, fund_score: float | None, row: dict) -> str:
    """매수 후보 판정.

    펀더멘털이 있으면 종합 점수, 없으면 기술적 점수만.
    """
    if fund_score is not None:
        combined = (tech_score + fund_score) / 2
    else:
        combined = tech_score

    if combined >= 70 and row.get('above_sma200'):
        return "관심"
    elif combined >= 55:
        return "보류"
    else:
        return "미충족"


def compute_damodaran_label(row: dict) -> str:
    """Damodaran식 라이프사이클 스토리 라벨 (휴리스틱)."""
    rev_g = row.get('rev_growth') or 0
    roe = row.get('roe') or 0
    market_cap = row.get('market_cap') or 0
    above_200 = row.get('above_sma200', False)
    dist_high = row.get('pct_from_52w_high') or -100
    ret_6m = row.get('return_6m') or 0

    # 데이터 부족시 가격 기반으로만
    if not row.get('has_fundamentals'):
        if above_200 and dist_high >= -5:
            return "추세형 강세"
        if ret_6m < -20:
            return "조정/턴어라운드 후보"
        return "관망"

    # 펀더멘털 기반
    if rev_g >= 25 and roe >= 15:
        return "초고성장기 (Growth)"
    if rev_g >= 15 and roe >= 12:
        return "성장기 (Expansion)"
    if rev_g >= 5 and roe >= 10:
        return "안정 성장 (Mature Growth)"
    if rev_g >= 0 and roe >= 8:
        return "안정기 (Mature)"
    if rev_g < 0 and ret_6m < -10:
        return "쇠퇴/턴어라운드 후보"
    return "안정기 (Mature)"


# ============================================================
#  스크리닝
# ============================================================

def screen_market(price_df: pd.DataFrame, fund_df: pd.DataFrame, market_label: str) -> pd.DataFrame:
    """가격 + 펀더멘털 머지 후 점수 부여."""
    if price_df.empty:
        return price_df

    df = price_df.copy()

    if not fund_df.empty:
        df = df.merge(fund_df, on='ticker', how='left')
    else:
        # 펀더멘털 컬럼 빈 값으로 추가
        for col in ['market_cap', 'pe', 'peg', 'pb', 'p_fcf', 'roe',
                    'debt_equity', 'rev_growth', 'eps_growth', 'dividend_yield',
                    'industry', 'has_fundamentals']:
            df[col] = None
        df['has_fundamentals'] = False

    # 점수 계산
    df['tech_score'] = df.apply(lambda r: compute_score(r.to_dict()), axis=1)
    df['fund_score'] = df.apply(lambda r: compute_fundamental_score(r.to_dict()), axis=1)
    df['combined_score'] = df.apply(
        lambda r: round((r['tech_score'] + (r['fund_score'] if pd.notna(r['fund_score']) else r['tech_score'])) / 2, 1),
        axis=1
    )
    df['verdict'] = df.apply(
        lambda r: compute_verdict(r['tech_score'],
                                   r['fund_score'] if pd.notna(r['fund_score']) else None,
                                   r.to_dict()),
        axis=1
    )
    df['damodaran'] = df.apply(lambda r: compute_damodaran_label(r.to_dict()), axis=1)
    df['market'] = market_label

    df = df.sort_values('combined_score', ascending=False).reset_index(drop=True)
    return df


def select_highlights(df: pd.DataFrame, n: int = 10) -> dict:
    """4가지 카테고리 상위 N."""
    if df.empty:
        return {'top_gainers': [], 'volume_surge': [], 'sma200_break': [], 'near_52w_high': []}

    gainers = df[df['change_pct'] > 0].sort_values('change_pct', ascending=False).head(n)
    surge = df[df['volume_ratio'] >= 1.5].sort_values('volume_ratio', ascending=False).head(n)
    break200 = df[df['above_sma200']].sort_values('combined_score', ascending=False).head(n)
    near_high = df[df['pct_from_52w_high'] >= -5].sort_values('pct_from_52w_high', ascending=False).head(n)

    return {
        'top_gainers': gainers.to_dict('records'),
        'volume_surge': surge.to_dict('records'),
        'sma200_break': break200.to_dict('records'),
        'near_52w_high': near_high.to_dict('records'),
    }


def select_top10_and_top3(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """TOP 10 (관심+보류 중 점수 상위 10) + TOP 3 (관심 중 점수 상위 3)."""
    if df.empty:
        return [], []

    # TOP 10: 200일선 위 + 점수 상위 10
    top10_df = df[df['above_sma200']].sort_values('combined_score', ascending=False).head(10)

    # TOP 3: 관심 판정 + 점수 상위 3
    top3_df = df[(df['verdict'] == '관심')].sort_values('combined_score', ascending=False).head(3)

    return top10_df.to_dict('records'), top3_df.to_dict('records')


# ============================================================
#  자산배분 의견
# ============================================================

def compute_allocation(vix: float | None) -> dict:
    """VIX 기반 자산배분 비율."""
    if vix is None:
        vix = 18.0  # 기본값

    if vix < 15:
        return {'stock': 70, 'gold': 10, 'cash': 20, 'note': '저변동성 — 위험자산 비중 확대 가능'}
    elif vix < 20:
        return {'stock': 60, 'gold': 15, 'cash': 25, 'note': '평균적 변동성 — 균형 배분'}
    elif vix < 30:
        return {'stock': 50, 'gold': 20, 'cash': 30, 'note': '변동성 확대 — 방어 비중 증가'}
    else:
        return {'stock': 35, 'gold': 25, 'cash': 40, 'note': '고변동성 — 현금 비중 우선'}


# ============================================================
#  카카오톡 / 블로그 글 생성
# ============================================================

def render_kakao_message(date_str: str, market_grp: dict, us_top3: list[dict], kr_top3: list[dict],
                          allocation: dict, us_top10: list[dict] = None, kr_top10: list[dict] = None,
                          stories: dict = None) -> str:
    """카카오톡 발송용 요약. TOP3 없으면 TOP10 상위 3개로 대체, Damodaran 해자·논리 포함."""
    stories = stories or {}
    # TOP3 있으면 TOP3, 없으면 TOP10 상위 3개
    us_picks = us_top3 if us_top3 else (us_top10 or [])[:3]
    kr_picks = kr_top3 if kr_top3 else (kr_top10 or [])[:3]

    sp = next((m for m in market_grp.get('us_index', []) if m['ticker'] == '^GSPC'), None)
    ks = next((m for m in market_grp.get('kr_index', []) if m['ticker'] == '^KS11'), None)
    vix = next((m for m in market_grp.get('macro', []) if m['ticker'] == '^VIX'), None)

    lines = [
        f"📈 에스아이스토리 리포트 ({date_str})",
        "",
    ]
    if sp:
        lines.append(f"• S&P 500: {sp['value_str']} ({'+' if sp['change_pct']>=0 else ''}{sp['change_pct']:.2f}%)")
    if ks:
        lines.append(f"• 코스피: {ks['value_str']} ({'+' if ks['change_pct']>=0 else ''}{ks['change_pct']:.2f}%)")
    if vix:
        lines.append(f"• VIX: {vix['value_str']}")

    def stock_block(r):
        st = stories.get(r['ticker'], {})
        blk = [f"  {r['ticker']} ({r.get('name','')[:20]}) — {r['combined_score']:.0f}점"]
        if st.get('thesis'):
            blk.append(f"   📖 {st['thesis']}")
        if st.get('moat'):
            blk.append(f"   🛡️ 해자: {st['moat']}")
        sup = st.get('suppliers') or []
        if sup:
            names = ', '.join(s.get('name','') for s in sup[:3])
            blk.append(f"   🔗 협력사: {names}")
        return blk

    lines.append("")
    if us_picks:
        lines.append("🇺🇸 미국 관심종목 TOP 3" if us_top3 else "🇺🇸 미국 상위 후보 3")
        for r in us_picks:
            lines.extend(stock_block(r))
            lines.append("")
    if kr_picks:
        lines.append("🇰🇷 한국 관심종목 TOP 3" if kr_top3 else "🇰🇷 한국 상위 후보 3")
        for r in kr_picks:
            lines.extend(stock_block(r))
            lines.append("")

    lines.append(f"💰 자산배분: 주식 {allocation['stock']}% / 금 {allocation['gold']}% / 현금 {allocation['cash']}%")
    lines.append("")
    lines.append("※ 본 정보는 투자 참고용입니다. 협력사는 AI 추정으로 부정확할 수 있습니다.")

    return '\n'.join(lines)


def render_blog_post(date_str: str, market_grp: dict, market_interpret: str,
                     us_top10: list[dict], kr_top10: list[dict],
                     us_top3: list[dict], kr_top3: list[dict],
                     allocation: dict, stories: dict = None) -> str:
    """블로그 업로드용 마크다운. TOP3 없으면 TOP10 상위 3개, Damodaran 분석 포함."""
    stories = stories or {}
    us_picks = us_top3 if us_top3 else (us_top10 or [])[:3]
    kr_picks = kr_top3 if kr_top3 else (kr_top10 or [])[:3]

    sp = next((m for m in market_grp.get('us_index', []) if m['ticker'] == '^GSPC'), None)
    ks = next((m for m in market_grp.get('kr_index', []) if m['ticker'] == '^KS11'), None)

    lines = [
        f"# {date_str} 시황 리포트",
        "",
        "## 오늘의 시장",
        "",
        market_interpret,
        "",
    ]
    if sp or ks:
        lines.append("| 지수 | 종가 | 등락률 |")
        lines.append("|---|---|---|")
        if sp:
            lines.append(f"| S&P 500 | {sp['value_str']} | {'+' if sp['change_pct']>=0 else ''}{sp['change_pct']:.2f}% |")
        if ks:
            lines.append(f"| 코스피 | {ks['value_str']} | {'+' if ks['change_pct']>=0 else ''}{ks['change_pct']:.2f}% |")
        lines.append("")

    def stock_section(r):
        st = stories.get(r['ticker'], {})
        sec = [
            f"### {r['ticker']} — {r.get('name','')}",
            "",
            f"- **종합 점수**: {r['combined_score']:.0f}점",
        ]
        if st.get('thesis'):
            sec.append(f"- **📖 Damodaran 투자 논리**: {st['thesis']}")
        if st.get('moat'):
            sec.append(f"- **🛡️ 해자 (Moat)**: {st['moat']}")
        if st.get('growth'):
            sec.append(f"- **📈 성장 동인**: {st['growth']}")
        if st.get('risk'):
            sec.append(f"- **⚠️ 리스크 요인**: {st['risk']}")
        if st.get('megatrend'):
            sec.append(f"- **🚀 메가트렌드**: {st['megatrend']}")
        sup = st.get('suppliers') or []
        if sup:
            names = ' / '.join(f"{s.get('name','')} ({s.get('reason','')})" for s in sup[:3])
            sec.append(f"- **🔗 대표 협력사**: {names}")
        if not st.get('thesis') and r.get('damodaran'):
            sec.append(f"- 스토리: {r.get('damodaran','')}")
        sec.append("")
        return sec

    us_title = "## 미국 관심 종목 TOP 3" if us_top3 else "## 미국 상위 후보 TOP 3"
    lines.append(us_title)
    lines.append("")
    if us_picks:
        for r in us_picks:
            lines.extend(stock_section(r))
    else:
        lines.append("_오늘은 조건을 충족하는 종목이 없습니다._\n")

    kr_title = "## 한국 관심 종목 TOP 3" if kr_top3 else "## 한국 상위 후보 TOP 3"
    lines.append(kr_title)
    lines.append("")
    if kr_picks:
        for r in kr_picks:
            lines.extend(stock_section(r))
    else:
        lines.append("_오늘은 조건을 충족하는 종목이 없습니다._\n")

    lines.append(f"## 자산 배분 의견")
    lines.append("")
    lines.append(f"- 주식: **{allocation['stock']}%**")
    lines.append(f"- 금: **{allocation['gold']}%**")
    lines.append(f"- 현금/채권: **{allocation['cash']}%**")
    lines.append("")
    lines.append(f"_{allocation['note']}_")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> **투자 유의사항**: 본 자료는 공개 시장 데이터 기반의 투자 참고용 정보로, 특정 종목의 매수·매도를 권유하지 않습니다. 협력사 정보는 AI 추정으로 부정확할 수 있습니다. 투자 판단과 손익 책임은 전적으로 투자자 본인에게 있습니다.")

    return '\n'.join(lines)


# ============================================================
#  표시 포맷
# ============================================================

def format_price(price: float, ticker: str) -> str:
    if ticker.endswith('.KS') or ticker.endswith('.KQ'):
        return f"₩{price:,.0f}"
    return f"${price:,.2f}"


def fmt_or_dash(v, fmt='{:.2f}', mult=1.0):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return '—'
    try:
        return fmt.format(float(v) * mult)
    except Exception:
        return '—'


def prepare_row(row: dict) -> dict:
    """템플릿용 dict."""
    breakdown = compute_score_breakdown(row)
    return {
        'ticker': row['ticker'],
        'name': str(row.get('name', ''))[:30],
        'sector': str(row.get('sector', ''))[:20],
        'industry': str(row.get('industry', '') or '')[:25],
        'price_str': format_price(row['price'], row['ticker']),
        'change_pct': row.get('change_pct', 0),
        'volume_ratio': row.get('volume_ratio', 0),
        'above_sma50': row.get('above_sma50', False),
        'above_sma200': row.get('above_sma200', False),
        'golden_cross': row.get('golden_cross', False),
        'pct_from_52w_high': row.get('pct_from_52w_high', 0),
        'volatility_20d': row.get('volatility_20d', 0),
        'return_3m': row.get('return_3m', 0),
        'return_6m': row.get('return_6m', 0),
        'tech_score': row.get('tech_score', 0),
        'fund_score': row.get('fund_score'),
        'combined_score': row.get('combined_score', 0),
        'verdict': row.get('verdict', '-'),
        'damodaran': row.get('damodaran', '-'),
        # 점수 분해
        'score_trend': breakdown['trend'],
        'score_momentum': breakdown['momentum'],
        'score_volume': breakdown['volume'],
        'score_position_52w': breakdown['position_52w'],
        'score_breakout': breakdown['breakout'],
        # 펀더멘털 (표시용 문자열)
        'pe_str': fmt_or_dash(row.get('pe'), '{:.1f}'),
        'peg_str': fmt_or_dash(row.get('peg'), '{:.2f}'),
        'pb_str': fmt_or_dash(row.get('pb'), '{:.2f}'),
        'p_fcf_str': fmt_or_dash(row.get('p_fcf'), '{:.1f}'),
        'roe_str': fmt_or_dash(row.get('roe'), '{:.1f}%'),
        'debt_equity_str': fmt_or_dash(row.get('debt_equity'), '{:.0f}'),
        'rev_growth_str': fmt_or_dash(row.get('rev_growth'), '{:+.1f}%'),
        'eps_growth_str': fmt_or_dash(row.get('eps_growth'), '{:+.1f}%'),
        'has_fundamentals': bool(row.get('has_fundamentals')),
        # 리스크 지표
        'mdd': row.get('mdd'),
        'sharpe': row.get('sharpe'),
        'beta': row.get('beta'),
        'alpha': row.get('alpha'),
        'var_95': row.get('var_95'),
        'risk_score': row.get('risk_score'),
        'mdd_str': fmt_or_dash(row.get('mdd'), '{:.1f}%'),
        'sharpe_str': fmt_or_dash(row.get('sharpe'), '{:.2f}'),
        'beta_str': fmt_or_dash(row.get('beta'), '{:.2f}'),
        'alpha_str': fmt_or_dash(row.get('alpha'), '{:+.1f}%'),
        'var_95_str': fmt_or_dash(row.get('var_95'), '{:.1f}%'),
        'risk_score_str': fmt_or_dash(row.get('risk_score'), '{:.0f}'),
    }


def prepare_highlights(highlights: dict) -> dict:
    return {k: [prepare_row(r) for r in v] for k, v in highlights.items()}


# ============================================================
#  HTML 렌더
# ============================================================

def render_template(template_name: str, **kwargs) -> str:
    from jinja2 import Environment, FileSystemLoader
    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(template_dir), autoescape=True)
    return env.get_template(template_name).render(**kwargs)


# ============================================================
#  메인 흐름
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--us-fundamentals-limit", type=int, default=0,
                        help="펀더멘털 수집할 미국 종목 수 제한 (0=전체)")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst)
    timestamp_str = today.strftime("%Y년 %m월 %d일 %H:%M 기준 · 투자 참고용")
    date_str = today.strftime("%Y년 %m월 %d일")

    log.info("=" * 60)
    log.info("  에스아이 스토리 — 일일 리포트 빌드")
    log.info("  %s", timestamp_str)
    log.info("=" * 60)

    # 1. 종목 풀 (자체 포함 데이터 — data 폴더 불필요)
    us_universe = load_us_universe()
    krx_universe = load_krx_universe()

    # 2. 시장 요약
    market_grp, market_interpret = fetch_market_summary()

    # 3. 가격 데이터 (양쪽 풀)
    us_prices, us_series_map = fetch_universe_prices(us_universe, "US")
    krx_prices, krx_series_map = fetch_universe_prices(krx_universe, "KR")

    # 시장(S&P 500) 종가 시리즈 - 베타/알파 계산용
    market_series = None
    try:
        spy_data = fetch_price_data("^GSPC")
        if spy_data:
            market_series = spy_data.get('_close_series')
    except Exception as e:
        log.warning("시장 시리즈 수집 실패: %s", e)

    # 3b. 리스크 지표 계산 (모든 종목)
    log.info("리스크 지표 계산...")
    try:
        from risk_metrics import compute_risk_for_all
        us_risk = compute_risk_for_all(us_series_map, market_series)
        krx_risk = compute_risk_for_all(krx_series_map, market_series)
    except Exception as e:
        log.warning("리스크 계산 실패: %s", e)
        us_risk = {}
        krx_risk = {}

    # 4. 펀더멘털 (미국만)
    us_fund_tickers = us_prices['ticker'].tolist() if not us_prices.empty else []
    if args.us_fundamentals_limit > 0:
        us_fund_tickers = us_fund_tickers[:args.us_fundamentals_limit]
    us_fund = fetch_universe_fundamentals(us_fund_tickers, "US")

    # 한국은 펀더멘털 빈 DF
    krx_fund = pd.DataFrame()

    # 5. 스크리닝 (리스크 지표 머지)
    us_screened = screen_market(us_prices, us_fund, "US")
    krx_screened = screen_market(krx_prices, krx_fund, "KR")

    # 리스크 컬럼 머지
    def _merge_risk(df, risk_map):
        if df.empty or not risk_map:
            return df
        risk_df = pd.DataFrame.from_dict(risk_map, orient='index').reset_index()
        risk_df = risk_df.rename(columns={'index': 'ticker'})
        return df.merge(risk_df, on='ticker', how='left')

    us_screened = _merge_risk(us_screened, us_risk)
    krx_screened = _merge_risk(krx_screened, krx_risk)

    # 6. 하이라이트 (4종 카테고리)
    us_hi_10 = select_highlights(us_screened, n=10)
    krx_hi_10 = select_highlights(krx_screened, n=10)
    us_hi_30 = select_highlights(us_screened, n=30)
    krx_hi_30 = select_highlights(krx_screened, n=30)

    # 7. TOP 10 / TOP 3
    us_top10, us_top3 = select_top10_and_top3(us_screened)
    kr_top10, kr_top3 = select_top10_and_top3(krx_screened)

    # 7b. 간이 백테스트 (미국 only, 1년 TOP10 전략 vs SPY)
    log.info("백테스트 실행...")
    try:
        from backtest import run_backtest
        # 미국 종목 가격 DataFrame (날짜 인덱스, 티커 컬럼)
        if us_series_map and market_series is not None:
            us_price_df = pd.DataFrame(us_series_map)
            backtest_result = run_backtest(us_price_df, market_series)
        else:
            log.warning("백테스트 데이터 부족 - 생략")
            backtest_result = None
    except Exception as e:
        log.warning("백테스트 실패: %s", e)
        backtest_result = None

    # 8. 자산배분
    vix_val = None
    vix_row = next((m for m in market_grp.get('macro', []) if m['ticker'] == '^VIX'), None)
    if vix_row:
        try:
            vix_val = float(vix_row['value_str'].replace(',', ''))
        except Exception:
            pass
    allocation = compute_allocation(vix_val)

    # 9. 통계
    stats = {
        'us_total': len(us_universe),
        'us_loaded': len(us_screened),
        'krx_total': len(krx_universe),
        'krx_loaded': len(krx_screened),
        'us_above_200': int(us_screened['above_sma200'].sum()) if not us_screened.empty else 0,
        'krx_above_200': int(krx_screened['above_sma200'].sum()) if not krx_screened.empty else 0,
        'us_fund_filled': int(us_screened['has_fundamentals'].sum()) if not us_screened.empty else 0,
    }

    # 9b. Damodaran 스토리 (TOP10 종목)
    log.info("Damodaran 스토리 생성...")
    try:
        from damodaran import build_stories
        from tickers_data import SECTOR_STORIES as sector_db
        us_stories = build_stories(us_top10, sector_db, use_llm=True)
        kr_stories = build_stories(kr_top10, sector_db, use_llm=True)
    except Exception as e:
        log.warning("스토리 생성 실패 (보고서는 계속): %s", e)
        us_stories = {}
        kr_stories = {}

    # 10. 카카오 / 블로그 (프리미엄용) — stories와 top10도 함께 넘김 (TOP3 없으면 TOP10 폴백)
    us_top10_rows = [prepare_row(r) for r in us_top10]
    kr_top10_rows = [prepare_row(r) for r in kr_top10]
    us_top3_rows  = [prepare_row(r) for r in us_top3]
    kr_top3_rows  = [prepare_row(r) for r in kr_top3]
    # 합친 stories (무료 페이지가 미국+한국 둘 다 쓰니까)
    all_stories = {**us_stories, **kr_stories}

    us_kakao = render_kakao_message(date_str, market_grp, us_top3_rows, [], allocation,
                                     us_top10=us_top10_rows, stories=us_stories)
    kr_kakao = render_kakao_message(date_str, market_grp, [], kr_top3_rows, allocation,
                                     kr_top10=kr_top10_rows, stories=kr_stories)
    us_blog = render_blog_post(date_str, market_grp, market_interpret,
                                us_top10_rows, [],
                                us_top3_rows, [],
                                allocation, stories=us_stories)
    kr_blog = render_blog_post(date_str, market_grp, market_interpret,
                                [], kr_top10_rows,
                                [], kr_top3_rows,
                                allocation, stories=kr_stories)
    # 무료(랜딩) 페이지용 — 미국+한국 통합 카카오/블로그
    free_kakao = render_kakao_message(date_str, market_grp, us_top3_rows, kr_top3_rows, allocation,
                                       us_top10=us_top10_rows, kr_top10=kr_top10_rows, stories=all_stories)
    free_blog = render_blog_post(date_str, market_grp, market_interpret,
                                  us_top10_rows, kr_top10_rows,
                                  us_top3_rows, kr_top3_rows,
                                  allocation, stories=all_stories)

    # 11. 보고서 렌더링

    # 11-1 무료 보고서 (TOP3 제외, 하이라이트만)
    log.info("무료 보고서 렌더링...")
    free_html = render_template(
        "free_template.html",
        timestamp_str=timestamp_str, date_str=date_str,
        market=market_grp, market_interpret=market_interpret,
        us_highlights=prepare_highlights(us_hi_10),
        krx_highlights=prepare_highlights(krx_hi_10),
        allocation=allocation,
        stats=stats,
        kakao=free_kakao, blog=free_blog,
    )
    free_path = args.output_dir / "si_investment_report.html"
    free_path.write_text(free_html, encoding='utf-8')
    log.info("  ✓ %s (%d bytes)", free_path.name, free_path.stat().st_size)

    # 11-2 미국 프리미엄
    log.info("미국 프리미엄 렌더링...")
    us_full = [prepare_row(r) for r in us_screened.head(50).to_dict('records')]
    us_prem_html = render_template(
        "us_premium_template.html",
        timestamp_str=timestamp_str, date_str=date_str,
        market=market_grp, market_interpret=market_interpret,
        highlights=prepare_highlights(us_hi_30),
        top10=[prepare_row(r) for r in us_top10],
        top3=[prepare_row(r) for r in us_top3],
        full=us_full,
        stories=us_stories,
        backtest=backtest_result,
        kakao=us_kakao, blog=us_blog,
        allocation=allocation,
        stats=stats,
    )
    us_path = args.output_dir / "us_premium_report.html"
    us_path.write_text(us_prem_html, encoding='utf-8')
    log.info("  ✓ %s (%d bytes)", us_path.name, us_path.stat().st_size)

    # 11-3 한국 프리미엄
    log.info("한국 프리미엄 렌더링...")
    kr_full = [prepare_row(r) for r in krx_screened.head(50).to_dict('records')]
    kr_prem_html = render_template(
        "korea_premium_template.html",
        timestamp_str=timestamp_str, date_str=date_str,
        market=market_grp, market_interpret=market_interpret,
        highlights=prepare_highlights(krx_hi_30),
        top10=[prepare_row(r) for r in kr_top10],
        top3=[prepare_row(r) for r in kr_top3],
        full=kr_full,
        stories=kr_stories,
        kakao=kr_kakao, blog=kr_blog,
        allocation=allocation,
        stats=stats,
    )
    kr_path = args.output_dir / "korea_premium_report.html"
    kr_path.write_text(kr_prem_html, encoding='utf-8')
    log.info("  ✓ %s (%d bytes)", kr_path.name, kr_path.stat().st_size)

    log.info("=" * 60)
    log.info("  완료. 미국 %d/%d, 한국 %d/%d, 미국 펀더멘털 %d개",
             stats['us_loaded'], stats['us_total'],
             stats['krx_loaded'], stats['krx_total'],
             stats['us_fund_filled'])
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
