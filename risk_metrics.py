"""
종목별 리스크 지표 계산.

  - MDD: 1년 최대낙폭
  - 샤프: 연환산 수익률 / 변동성
  - 베타: vs S&P 500 (^GSPC)
  - VaR 95%: 일간 95% 신뢰구간 손실
  - 알파: 1년 수익률 - β × 시장 수익률
  - 종합 리스크 점수: MDD + 변동성 + 베타 통합 (낮을수록 안전)
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

RISK_FREE_RATE = 0.045  # 미국 단기 국채 약 4.5% 가정
TRADING_DAYS = 252


def compute_risk_metrics(close_series: pd.Series,
                          market_series: pd.Series | None = None) -> dict:
    """단일 종목 가격 시리즈에서 리스크 지표 계산.

    Args:
        close_series: 일간 종가 시리즈 (인덱스: 날짜)
        market_series: 시장 지수 종가 (베타·알파 계산용). None이면 베타/알파 생략.

    반환:
        {
          'mdd': float,         # 최대낙폭 %
          'sharpe': float,      # 샤프 비율
          'volatility': float,  # 연환산 변동성 %
          'beta': float | None,
          'alpha': float | None, # 연환산 % (CAPM)
          'var_95': float,       # 95% 신뢰구간 일간 손실 %
          'ret_1y': float,       # 1년 누적 수익률 %
          'risk_score': float,   # 0~100 (낮을수록 안전)
        }
    """
    if close_series is None or len(close_series) < 30:
        return _empty_metrics()

    close = pd.Series(close_series).dropna()
    if len(close) < 30:
        return _empty_metrics()

    # 일간 수익률
    daily_returns = close.pct_change().dropna()
    if len(daily_returns) < 20:
        return _empty_metrics()

    # 1년 누적 수익률
    ret_1y = (close.iloc[-1] / close.iloc[0] - 1) * 100

    # 변동성 (연환산 %)
    vol_annual = float(daily_returns.std() * np.sqrt(TRADING_DAYS) * 100)

    # MDD (최대낙폭)
    cum = (1 + daily_returns).cumprod()
    running_max = cum.cummax()
    drawdown = (cum / running_max - 1) * 100
    mdd = float(drawdown.min())  # 음수

    # 샤프 비율 (연환산)
    excess_ret_annual = (1 + daily_returns.mean()) ** TRADING_DAYS - 1 - RISK_FREE_RATE
    sharpe = float(excess_ret_annual / (daily_returns.std() * np.sqrt(TRADING_DAYS))) \
        if daily_returns.std() > 0 else 0.0

    # VaR 95% (일간 손실)
    var_95 = float(np.percentile(daily_returns, 5) * 100)  # 음수

    # 베타·알파
    beta = None
    alpha = None
    if market_series is not None and len(market_series) >= 30:
        try:
            mkt = pd.Series(market_series).dropna()
            mkt_ret = mkt.pct_change().dropna()

            # 같은 날짜로 정렬
            aligned = pd.concat(
                [daily_returns.rename('stock'), mkt_ret.rename('mkt')],
                axis=1, join='inner'
            ).dropna()

            if len(aligned) >= 30 and aligned['mkt'].var() > 0:
                cov = aligned['stock'].cov(aligned['mkt'])
                var_mkt = aligned['mkt'].var()
                beta = float(cov / var_mkt)

                # 알파 (연환산 CAPM)
                stock_ret_annual = (1 + aligned['stock'].mean()) ** TRADING_DAYS - 1
                mkt_ret_annual = (1 + aligned['mkt'].mean()) ** TRADING_DAYS - 1
                alpha = float((stock_ret_annual - RISK_FREE_RATE
                              - beta * (mkt_ret_annual - RISK_FREE_RATE)) * 100)
        except Exception as e:
            log.debug("베타/알파 계산 실패: %s", e)

    # 종합 리스크 점수 (낮을수록 안전, 0~100)
    risk_score = _compute_risk_score(vol_annual, mdd, beta)

    return {
        'mdd': round(mdd, 2),
        'sharpe': round(sharpe, 2),
        'volatility': round(vol_annual, 1),
        'beta': round(beta, 2) if beta is not None else None,
        'alpha': round(alpha, 2) if alpha is not None else None,
        'var_95': round(var_95, 2),
        'ret_1y': round(ret_1y, 2),
        'risk_score': round(risk_score, 1),
    }


def _empty_metrics() -> dict:
    return {
        'mdd': None, 'sharpe': None, 'volatility': None,
        'beta': None, 'alpha': None, 'var_95': None,
        'ret_1y': None, 'risk_score': None,
    }


def _compute_risk_score(vol: float, mdd: float, beta: float | None) -> float:
    """종합 리스크 점수 0~100. 낮을수록 안전.

    - 변동성 30% 가중: 15% 미만 0점, 60% 이상 30점
    - MDD 40% 가중: -10% 미만 0점, -50% 이하 40점
    - 베타 30% 가중: 0.5~1.2 적정 0점, 그 외 가산 (없으면 변동성으로 대체)
    """
    score = 0.0

    # 변동성 (0~30)
    if vol <= 15:
        score += 0
    elif vol >= 60:
        score += 30
    else:
        score += (vol - 15) / 45 * 30

    # MDD (0~40, mdd는 음수)
    mdd_abs = abs(mdd)
    if mdd_abs <= 10:
        score += 0
    elif mdd_abs >= 50:
        score += 40
    else:
        score += (mdd_abs - 10) / 40 * 40

    # 베타 (0~30)
    if beta is None:
        # 베타 없으면 변동성으로 대체 가산
        score += min((vol - 25) / 35 * 30, 30) if vol > 25 else 0
    else:
        # 0.8~1.2면 0점, 멀어질수록 가산
        if 0.8 <= beta <= 1.2:
            score += 0
        elif beta < 0.8:
            score += min((0.8 - beta) * 20, 30)
        else:
            score += min((beta - 1.2) * 20, 30)

    return min(max(score, 0), 100)


def compute_risk_for_all(price_data: dict[str, pd.Series],
                          market_close: pd.Series | None) -> dict[str, dict]:
    """전체 종목에 대해 리스크 지표 일괄 계산.

    Args:
        price_data: {ticker: close_series}
        market_close: 시장 지수 종가 시리즈

    Returns:
        {ticker: metrics_dict}
    """
    log.info("리스크 지표 계산: %d 종목", len(price_data))
    out = {}
    for ticker, close in price_data.items():
        out[ticker] = compute_risk_metrics(close, market_close)
    return out
