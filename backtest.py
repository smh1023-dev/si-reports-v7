"""
간이 백테스트.

전략: 매일 점수 상위 10개 종목을 동일가중 보유.
      다음날 점수가 떨어진 종목은 새 TOP10로 교체.
기간: 지난 1년 (252 거래일)
비교: SPY 1년 누적수익률.

성능 보호: 결과 캐시 (주 1회만 신규 계산, 다른 요일은 캐시 반환).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / ".backtest_cache" / "result.json"
CACHE_DAYS = 7  # 주 1회 갱신
TRADING_DAYS = 252
TOP_N = 10  # 매일 보유 종목 수


# ============================================================
#  캐시
# ============================================================

def _load_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        ts = datetime.fromisoformat(data['_cached_at'])
        if datetime.now() - ts < timedelta(days=CACHE_DAYS):
            log.info("백테스트 캐시 사용 (생성: %s)", ts.strftime('%Y-%m-%d'))
            return data
    except Exception:
        pass
    return None


def _save_cache(data: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data['_cached_at'] = datetime.now().isoformat()
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        log.warning("백테스트 캐시 저장 실패: %s", e)


# ============================================================
#  간이 점수 함수 (백테스트용 - 과거 시점 기준)
# ============================================================

def _score_at_date(prices: pd.DataFrame, date_idx: int) -> pd.Series:
    """date_idx 시점에서의 각 종목 점수를 계산.

    Args:
        prices: 가격 DataFrame (인덱스 날짜, 컬럼 티커)
        date_idx: 점수 계산할 날짜 인덱스

    Returns:
        종목별 점수 Series
    """
    if date_idx < 200:
        return pd.Series(dtype=float)

    # 가격 윈도우
    window = prices.iloc[max(0, date_idx-251):date_idx+1]
    if len(window) < 50:
        return pd.Series(dtype=float)

    scores = {}
    for ticker in window.columns:
        col = window[ticker].dropna()
        if len(col) < 50:
            continue

        close = col.iloc[-1]
        if close <= 0:
            continue

        # SMA50, SMA200
        sma50 = col.tail(50).mean()
        sma200 = col.tail(200).mean() if len(col) >= 200 else None

        # 모멘텀: 3개월(63일) 수익률
        if len(col) >= 63:
            ret_3m = (close / col.iloc[-63] - 1) * 100
        else:
            ret_3m = 0

        # 52주 위치
        recent = col.tail(min(252, len(col)))
        high_52 = recent.max()
        pct_from_high = (close / high_52 - 1) * 100 if high_52 > 0 else 0

        # 단순 점수
        score = 0
        if close > sma50:
            score += 12
        if sma200 is not None and close > sma200:
            score += 20
        score += min(max(ret_3m, 0), 30) / 30 * 20
        dist = abs(pct_from_high)
        if dist <= 5:
            score += 15
        elif dist <= 10:
            score += 10

        scores[ticker] = score

    return pd.Series(scores)


# ============================================================
#  백테스트 메인
# ============================================================

def run_backtest(prices: pd.DataFrame, spy_prices: pd.Series) -> dict:
    """1년 백테스트 실행.

    Args:
        prices: 종목별 1년+ 가격 DataFrame
        spy_prices: SPY 가격 시리즈

    Returns:
        {
          strategy_cum: [...],  # 전략 누적수익률 % 시리즈
          spy_cum: [...],       # SPY 누적수익률 %
          dates: [...],         # 날짜 문자열
          summary: {final_strategy, final_spy, alpha, sharpe, mdd, max_holdings_change},
        }
    """
    # 캐시 우선
    cached = _load_cache()
    if cached:
        return cached

    log.info("백테스트 신규 실행 (1년)...")
    t0 = time.time()

    # 1년 백테스트 기간 설정
    n_days = len(prices)
    # 최소 200일 (SMA200 계산 위해) 이전부터 점수 계산 가능
    # 백테스트는 마지막 252일 기간 동안 실행
    start_idx = max(200, n_days - TRADING_DAYS)

    if start_idx >= n_days - 30:
        log.warning("데이터 부족 - 백테스트 생략")
        return _empty_result()

    # SPY 동기화
    spy = pd.Series(spy_prices).dropna()
    if len(spy) < 100:
        log.warning("SPY 데이터 부족")
        return _empty_result()

    # 인덱스를 prices와 맞춤
    common_dates = prices.index.intersection(spy.index)
    if len(common_dates) < 100:
        log.warning("SPY 날짜 매칭 부족")
        return _empty_result()
    prices_aligned = prices.loc[common_dates]
    spy_aligned = spy.loc[common_dates]

    n_aligned = len(prices_aligned)
    start_idx = max(200, n_aligned - TRADING_DAYS)

    strategy_daily_ret = []
    dates_list = []
    holdings_history = []

    current_holdings = None

    for idx in range(start_idx, n_aligned - 1):
        date = prices_aligned.index[idx]

        # 매주 월요일에만 리밸런싱 (속도)
        if current_holdings is None or date.weekday() == 0:
            scores = _score_at_date(prices_aligned, idx)
            if len(scores) >= TOP_N:
                top = scores.nlargest(TOP_N).index.tolist()
                current_holdings = top

        if current_holdings is None:
            strategy_daily_ret.append(0)
            dates_list.append(str(date.date()))
            continue

        # 다음날 수익률
        valid = [t for t in current_holdings if t in prices_aligned.columns]
        if not valid:
            strategy_daily_ret.append(0)
        else:
            today_prices = prices_aligned[valid].iloc[idx]
            tomorrow_prices = prices_aligned[valid].iloc[idx+1]
            returns = (tomorrow_prices / today_prices - 1).dropna()
            avg_ret = float(returns.mean()) if len(returns) > 0 else 0
            strategy_daily_ret.append(avg_ret)

        dates_list.append(str(date.date()))
        holdings_history.append(len(current_holdings))

    # 누적 수익률 시리즈
    strategy_ret_series = pd.Series(strategy_daily_ret)
    strategy_cum = ((1 + strategy_ret_series).cumprod() - 1) * 100

    # SPY 누적
    spy_in_period = spy_aligned.iloc[start_idx:start_idx + len(strategy_daily_ret) + 1]
    spy_ret = spy_in_period.pct_change().dropna()
    spy_cum = ((1 + spy_ret).cumprod() - 1) * 100

    # 길이 맞추기
    min_len = min(len(strategy_cum), len(spy_cum))
    strategy_cum = strategy_cum.iloc[:min_len].tolist()
    spy_cum_list = spy_cum.iloc[:min_len].tolist()
    dates_list = dates_list[:min_len]

    # 요약 통계
    final_strategy = strategy_cum[-1] if strategy_cum else 0
    final_spy = spy_cum_list[-1] if spy_cum_list else 0
    alpha = final_strategy - final_spy

    # MDD of strategy
    strat_arr = np.array(strategy_cum) / 100 + 1
    if len(strat_arr) > 0:
        running_max = np.maximum.accumulate(strat_arr)
        mdd = float(((strat_arr / running_max) - 1).min() * 100)
    else:
        mdd = 0

    # 샤프
    if len(strategy_daily_ret) > 20:
        daily = np.array(strategy_daily_ret)
        std = daily.std()
        sharpe = float((daily.mean() * 252 - 0.045) / (std * np.sqrt(252))) if std > 0 else 0
    else:
        sharpe = 0

    elapsed = time.time() - t0
    log.info("백테스트 완료: %.1f초, 거래일 %d, 최종 알파 %.1f%%",
             elapsed, len(strategy_cum), alpha)

    result = {
        'strategy_cum': [round(x, 2) for x in strategy_cum],
        'spy_cum': [round(x, 2) for x in spy_cum_list],
        'dates': dates_list,
        'summary': {
            'final_strategy': round(final_strategy, 2),
            'final_spy': round(final_spy, 2),
            'alpha': round(alpha, 2),
            'sharpe': round(sharpe, 2),
            'mdd': round(mdd, 2),
            'trading_days': len(strategy_cum),
        }
    }

    _save_cache(result)
    return result


def _empty_result() -> dict:
    return {
        'strategy_cum': [],
        'spy_cum': [],
        'dates': [],
        'summary': {
            'final_strategy': 0, 'final_spy': 0, 'alpha': 0,
            'sharpe': 0, 'mdd': 0, 'trading_days': 0,
        }
    }
