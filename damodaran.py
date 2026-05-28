"""
Damodaran 스타일 풍부한 종목 분석 모듈.

각 종목에 대해 5+2 항목을 생성:
  ① 핵심 사업 / 성장 동인  (LLM)
  ② 경제적 해자             (LLM)
  ③ 리스크 요인             (LLM)
  ④ 메가트렌드 (AI/플랫폼)  (LLM)
  ⑤ Damodaran 투자 논리     (LLM - 한 줄 종합)
  ⑥ 위험 변화 (정량)
  ⑦ 밸류에이션 (정량)

LLM 호출은 캐시(3일) + 폴백 처리.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / ".story_cache"
CACHE_DAYS = 3

# Claude API 설정 — Haiku 4.5 (저렴)
MODEL_NAME = "claude-haiku-4-5"
MAX_TOKENS_PER_STOCK = 800
TIMEOUT_SECONDS = 30


# ============================================================
#  캐시
# ============================================================

def _cache_path(ticker: str) -> Path:
    safe = ticker.replace('.', '_').replace('-', '_').replace('=', '_')
    return CACHE_DIR / f"{safe}.json"


def _load_cache(ticker: str) -> dict | None:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    try:
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        ts = datetime.fromisoformat(data['_cached_at'])
        if datetime.now() - ts < timedelta(days=CACHE_DAYS):
            return data
    except Exception:
        pass
    return None


def _save_cache(ticker: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data['_cached_at'] = datetime.now().isoformat()
    try:
        with open(_cache_path(ticker), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("캐시 저장 실패 %s: %s", ticker, e)


# ============================================================
#  ③ 위험 변화 (정량)
# ============================================================

def compute_risk_change(row: dict) -> dict:
    de = row.get('debt_equity')
    rev_g = row.get('rev_growth')
    eps_g = row.get('eps_growth')
    above_200 = row.get('above_sma200', False)
    ret_6m = row.get('return_6m', 0)

    signals = []
    score = 0

    if de is not None:
        if de < 50:
            signals.append("부채 낮음(D/E<50)")
            score += 1
        elif de > 150:
            signals.append("⚠ 부채 높음")

    if rev_g is not None:
        if rev_g >= 10:
            signals.append(f"매출 +{rev_g:.0f}%")
            score += 1
        elif rev_g < 0:
            signals.append(f"⚠ 매출 {rev_g:.0f}%")

    if eps_g is not None:
        if eps_g >= 10:
            signals.append(f"EPS +{eps_g:.0f}%")
            score += 1
        elif eps_g < -10:
            signals.append(f"⚠ EPS {eps_g:.0f}%")

    if not signals:
        if above_200 and ret_6m > 0:
            signals.append(f"6M 추세 +{ret_6m:.0f}%")
            score = 2
        elif ret_6m < -10:
            signals.append(f"⚠ 6M {ret_6m:.0f}%")

    if score >= 2:
        verdict = "위험 감소 신호"
    elif score == 1:
        verdict = "혼재"
    else:
        verdict = "위험 신호"

    return {
        'verdict': verdict,
        'signals': ' · '.join(signals) if signals else '데이터 부족',
        'score': score,
    }


# ============================================================
#  ④ 밸류에이션 (정량)
# ============================================================

def compute_valuation_check(row: dict) -> dict:
    peg = row.get('peg')
    p_fcf = row.get('p_fcf')
    pe = row.get('pe')

    checks = []
    pass_count = 0
    total_checks = 0

    if peg is not None and peg > 0:
        total_checks += 1
        if peg < 1.5:
            checks.append(f"PEG {peg:.2f} ✓")
            pass_count += 1
        else:
            checks.append(f"PEG {peg:.2f} ⚠")

    if p_fcf is not None and p_fcf > 0:
        total_checks += 1
        if p_fcf < 25:
            checks.append(f"P/FCF {p_fcf:.0f} ✓")
            pass_count += 1
        else:
            checks.append(f"P/FCF {p_fcf:.0f} ⚠")

    if pe is not None and pe > 0:
        total_checks += 1
        if pe < 20:
            checks.append(f"PER {pe:.0f} ✓")
            pass_count += 1
        elif pe > 40:
            checks.append(f"PER {pe:.0f} ⚠")
        else:
            checks.append(f"PER {pe:.0f}")
            pass_count += 0.5

    if total_checks == 0:
        return {'verdict': '데이터 부족', 'checks': '', 'pass_ratio': 0}

    ratio = pass_count / total_checks
    if ratio >= 0.7:
        verdict = "저평가 구간"
    elif ratio >= 0.4:
        verdict = "적정 구간"
    else:
        verdict = "고평가 구간"

    return {
        'verdict': verdict,
        'checks': ' · '.join(checks),
        'pass_ratio': ratio,
    }


# ============================================================
#  ①②④⑤ LLM 호출 - 풍부한 5항목 분석
# ============================================================

LLM_PROMPT_TEMPLATE = """당신은 Damodaran 스타일의 가치투자 애널리스트입니다.
다음 종목을 분석해주세요.

종목: {name} ({ticker})
섹터: {sector}
{extra}

6가지 항목을 다음 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 금지.
각 항목은 한국어로 작성하고, 구체적 사업/제품/경쟁사를 언급하세요.

{{
  "growth": "이 회사의 핵심 사업과 성장 동인을 한 문장(80-120자)으로. 구체적 사업명·제품·시장 언급. 예: 'Azure 클라우드와 Microsoft 365 Copilot의 AI 통합으로 기업 IT 인프라 표준 지위 강화 중'",
  "moat": "경제적 해자 한 문장(80-120자). 네트워크 효과/브랜드/전환비용/데이터/규모/특허 중 무엇인지 명시. 예: '30억+ 사용자 네트워크 효과와 광고주 데이터 락인으로 디지털 광고 시장 과점 구조'",
  "risk": "주요 리스크 요인 한 문장(80-120자). 규제·경쟁·기술변화·매크로 등 구체적으로. 예: 'EU/미국 빅테크 규제 강화와 TikTok 등 SNS 경쟁 심화가 광고 단가 하방 압력으로 작용'",
  "megatrend": "AI·데이터센터·플랫폼·네트워크효과·전기차·바이오 등 메가트렌드 관련성 한 문장(80-120자). 예: 'AI 학습 인프라(GPU)와 추천 알고리즘 자체 개발로 AI 인프라+서비스 양면 수혜'",
  "thesis": "Damodaran 스타일 한 줄 투자 논리(60-100자). 가치/성장/리스크 균형 관점. 예: 'AI 광고 효율 + 메타버스 옵션 가치를 합쳐 PEG 대비 저평가, 단 규제 리스크 모니터링 필수'",
  "suppliers": [
    {{"name": "협력사명 (티커)", "reason": "이 회사에 무엇을 납품하는지 한 줄(30-50자)"}},
    {{"name": "협력사명 (티커)", "reason": "납품 내용 한 줄"}}
  ]
}}

suppliers는 이 종목({name})에 부품·소재·서비스를 납품하는 '상장된' 대표 협력사 2~3개입니다.
반드시 실제로 상장된 회사만, 티커를 괄호 안에 표기하세요. 확실하지 않으면 적게 넣으세요. 추측성 종목은 절대 넣지 마세요."""


def call_llm_for_stock(client, ticker: str, name: str, sector: str,
                        extra_context: str = '') -> dict | None:
    """Claude로 5항목 분석 받기."""
    try:
        prompt = LLM_PROMPT_TEMPLATE.format(
            ticker=ticker, name=name, sector=sector,
            extra=f"재무 정보: {extra_context}" if extra_context else ""
        )
        message = client.messages.create(
            model=MODEL_NAME,
            max_tokens=MAX_TOKENS_PER_STOCK,
            messages=[{"role": "user", "content": prompt}],
            timeout=TIMEOUT_SECONDS,
        )
        text = message.content[0].text.strip()

        # JSON 추출 (코드블록 제거)
        if '```' in text:
            parts = text.split('```')
            for p in parts:
                p = p.strip()
                if p.startswith('json'):
                    p = p[4:].strip()
                if p.startswith('{'):
                    text = p
                    break
        text = text.strip()
        if not text.startswith('{'):
            # 첫 { 부터 마지막 } 까지
            start = text.find('{')
            end = text.rfind('}')
            if start >= 0 and end > start:
                text = text[start:end+1]

        data = json.loads(text)
        if not isinstance(data, dict):
            return None

        required = ['growth', 'moat', 'risk', 'megatrend', 'thesis']
        if not all(k in data for k in required):
            return None

        # 협력사 파싱 (선택 항목, 최대 3개)
        suppliers = []
        raw_sup = data.get('suppliers', [])
        if isinstance(raw_sup, list):
            for s in raw_sup[:3]:
                if isinstance(s, dict) and s.get('name'):
                    suppliers.append({
                        'name': str(s.get('name', ''))[:60],
                        'reason': str(s.get('reason', ''))[:120],
                    })

        return {
            'growth': str(data['growth'])[:300],
            'moat': str(data['moat'])[:300],
            'risk': str(data['risk'])[:300],
            'megatrend': str(data['megatrend'])[:300],
            'thesis': str(data['thesis'])[:250],
            'suppliers': suppliers,
        }
    except Exception as e:
        log.warning("LLM 실패 %s: %s", ticker, e)
        return None


def _fallback_story(ticker: str, name: str, sector: str,
                    sector_db: dict) -> dict:
    """LLM 실패 시 산업 기반 폴백."""
    sec_info = sector_db.get('sector_stories', {}).get(sector)
    if not sec_info:
        sec_info = sector_db.get('korea_sector_stories', {}).get(sector)

    if sec_info:
        return {
            'growth': f"[{sector}] {sec_info['growth_drivers']}",
            'moat': f"[일반 해자 패턴] {sec_info['moat_pattern']}",
            'risk': f"[{sector}] 산업 사이클, 경쟁 강도, 규제 환경 변화에 따른 변동성",
            'megatrend': "산업별 메가트렌드 관련성은 별도 검토 필요",
            'thesis': f"[{sector}] 산업 평균 동인. 종목별 차별화 요인 검토 후 진입 판단 필요.",
            'suppliers': [],
        }
    return {
        'growth': f"[{sector or '기타'}] 산업 트렌드에 따라 상이",
        'moat': "별도 검토 필요",
        'risk': "산업 평균 리스크 적용",
        'megatrend': "별도 검토 필요",
        'thesis': "산업 평균 - 종목별 분석 후 판단 필요",
        'suppliers': [],
    }


# ============================================================
#  메인 진입점
# ============================================================

def build_stories(rows: list[dict], sector_db: dict,
                  use_llm: bool = True) -> dict[str, dict]:
    """종목별 7-part 스토리 일괄 생성.

    Returns:
        {ticker: {growth, moat, risk, megatrend, thesis, risk_change, valuation}}
    """
    out = {}
    if not rows:
        return out

    client = None
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if use_llm and api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            log.info("✓ Anthropic 클라이언트 준비 완료 (모델 %s, 캐시 %d일)",
                     MODEL_NAME, CACHE_DAYS)
        except Exception as e:
            log.warning("Anthropic 초기화 실패: %s", e)
            client = None
    else:
        if not api_key:
            log.warning("ANTHROPIC_API_KEY 없음 - 산업 기반 폴백 사용")

    llm_called = 0
    cache_hit = 0
    fallback_used = 0

    for row in rows:
        ticker = row['ticker']
        name = row.get('name', '')
        sector = row.get('sector', '')

        cached = _load_cache(ticker)
        if cached:
            llm_part = {
                'growth': cached.get('growth'),
                'moat': cached.get('moat'),
                'risk': cached.get('risk', cached.get('moat', '')),
                'megatrend': cached.get('megatrend', ''),
                'thesis': cached.get('thesis', ''),
                'suppliers': cached.get('suppliers', []),
            }
            # 구버전 캐시 호환 (협력사 항목 없으면 새로 호출)
            if not cached.get('risk') or not cached.get('megatrend') or 'suppliers' not in cached:
                # 새 형식으로 재호출 필요
                cached = None

        if cached:
            cache_hit += 1
        elif client is not None:
            # 재무 정보를 LLM에 함께 전달
            extra = []
            if row.get('roe') is not None:
                extra.append(f"ROE {row['roe']:.0f}%")
            if row.get('rev_growth') is not None:
                extra.append(f"매출성장 {row['rev_growth']:+.0f}%")
            if row.get('eps_growth') is not None:
                extra.append(f"EPS성장 {row['eps_growth']:+.0f}%")
            if row.get('pe') is not None:
                extra.append(f"PER {row['pe']:.0f}")
            if row.get('peg') is not None:
                extra.append(f"PEG {row['peg']:.2f}")
            if row.get('debt_equity') is not None:
                extra.append(f"D/E {row['debt_equity']:.0f}")
            extra_context = ', '.join(extra)

            llm_result = call_llm_for_stock(client, ticker, name, sector, extra_context)
            if llm_result:
                llm_part = llm_result
                _save_cache(ticker, llm_result)
                llm_called += 1
            else:
                llm_part = _fallback_story(ticker, name, sector, sector_db)
                fallback_used += 1
        else:
            llm_part = _fallback_story(ticker, name, sector, sector_db)
            fallback_used += 1

        # 정량 분석 (LLM과 무관)
        risk_change = compute_risk_change(row)
        valuation = compute_valuation_check(row)

        out[ticker] = {
            'growth': llm_part['growth'],
            'moat': llm_part['moat'],
            'risk': llm_part.get('risk', ''),
            'megatrend': llm_part.get('megatrend', ''),
            'thesis': llm_part.get('thesis', ''),
            'suppliers': llm_part.get('suppliers', []),
            'risk_change': risk_change,
            'valuation': valuation,
        }

    log.info("Damodaran 스토리: LLM %d / 캐시 %d / 폴백 %d (총 %d종목)",
             llm_called, cache_hit, fallback_used, len(rows))

    return out
