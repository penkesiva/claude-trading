"""
agent/decision.py
Claude agent: web search + options data → trade decision
Uses Anthropic API with web_search tool + custom tools
"""

import json
import re
import anthropic
from datetime import datetime, date
import pytz

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from prompts.system_prompt import get_system_prompt, MORNING_SCAN_PROMPT, ENTRY_DECISION_PROMPT, POSITION_MONITOR_PROMPT
from tools.alpaca_tools import get_spy_options_chain, get_options_snapshot, get_options_chain_live, get_positions
from tools.market_tools import get_signal_snapshot
from tools.logger import log_decision, log_news, log_error
from tools.trump_tools import get_overnight_trump_posts, check_new_trump_posts


def _canonical_expiry(s: str) -> str:
    """Normalize expiry strings for comparison (YYYY-MM-DD)."""
    if not s:
        return ""
    t = str(s).strip().replace("/", "-")
    return t[:10] if len(t) >= 10 else t


client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# ── Web search tool definition ─────────────────────────────
# max_uses caps searches per API call to control costs
# Morning scan: allow 5 searches, Entry decision: allow 2
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",   # stable version
    "name": "web_search",
    "max_uses": 5,
}

WEB_SEARCH_TOOL_QUICK = {
    "type": "web_search_20250305",   # stable version
    "name": "web_search",
    "max_uses": 2,
}

# ──────────────────────────────────────────────────────────
# MORNING SCAN
# ──────────────────────────────────────────────────────────
def run_morning_scan() -> dict:
    """
    Deep morning scan: web search for news + form day thesis.
    Called once at 6:15 AM PST.
    """
    pst = pytz.timezone(config.TIMEZONE)
    now = datetime.now(pst)
    market_open = now.replace(hour=6, minute=30, second=0)
    minutes_to_open = max(0, int((market_open - now).total_seconds() / 60))

    prompt = MORNING_SCAN_PROMPT.format(
        time_pst=now.strftime("%H:%M"),
        minutes_to_open=minutes_to_open,
        date=date.today().strftime("%B %d, %Y"),
    )

    print("🌅 Running morning scan with web search...")

    # Fetch Trump's overnight posts (free, ~$0.00006)
    trump_data = get_overnight_trump_posts()
    if trump_data.get("available") and trump_data.get("market_relevant", 0) > 0:
        print(f"   🔴 Trump posts: {trump_data['summary']}")
        # Append Trump context to morning prompt
        trump_context = f"""
Also consider these recent Trump Truth Social posts (last 12 hours):
{trump_data['summary']}
Posts: {[p['text'][:150] for p in trump_data.get('flagged_posts', [])[:3]]}
"""
        prompt = prompt + trump_context
    elif trump_data.get("available"):
        print(f"   ✅ Trump posts: {trump_data['summary']}")
    else:
        print(f"   ⚠️  Trump posts: unavailable (add APIFY_API_KEY to .env)")

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=2000,
            system=get_system_prompt(config.PAPER_MODE),
            tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text from all content blocks
        thesis_text = _extract_text(response.content)

        # Parse JSON from response
        thesis = _extract_json(thesis_text)
        if not thesis:
            # Claude gave a good response but not JSON — wrap it
            thesis = {
                "bias":     _detect_bias(thesis_text),
                "thesis":   thesis_text[:300],
                "confidence": 0.6,
                "recommended_strategy": "claude_to_decide",
                "key_catalysts": [],
                "avoid_windows": [],
            }

        log_news("morning_deep_scan", [], thesis.get("thesis", "")[:200])
        bias = thesis.get("bias", "neutral").upper()
        snippet = thesis.get("thesis", "")[:80]
        print(f"✅ Morning thesis: {bias} | {snippet}...")
        return thesis

    except Exception as e:
        log_error("morning_scan", str(e))
        print(f"❌ Morning scan failed: {e}")
        return {"bias": "neutral", "thesis": "scan failed", "confidence": 0.3}

# ──────────────────────────────────────────────────────────
# ENTRY DECISION
# ──────────────────────────────────────────────────────────
def get_entry_decision(morning_thesis: dict, daily_pnl: float = 0, trades_today: int = 0) -> dict:
    """
    Claude decides whether to enter a trade.
    Gets live signals + options chain, then asks Claude.
    """
    print("🔍 Getting entry decision from Claude...")

    # Gather live data
    signals = get_signal_snapshot()

    # Use ticker + expiry from favorites.txt (set in main.py from favorites.txt)
    ticker = getattr(config, "ACTIVE_TICKER", "SPY")
    expiry = _canonical_expiry(getattr(config, "ACTIVE_EXPIRY", "") or "")
    expiry = expiry or None
    dte    = getattr(config, "ACTIVE_DTE", 0)

    # Live chain for favorites expiry only (avoids wrong-DTE chain)
    chain = get_options_chain_live(
        underlying=ticker,
        strike_range=8,
        expiration_date=expiry,
    )
    if not chain or not chain.get("contracts"):
        chain = get_spy_options_chain(
            expiry_date=expiry or date.today().strftime("%Y-%m-%d"),
            strike_range=8,
            underlying=ticker,
        )

    # Enrich with snapshots if available
    if chain.get("contracts") and not chain.get("error"):
        atm_symbols = [c["symbol"] for c in chain["contracts"][:10] if c.get("symbol")]
        if atm_symbols:
            snapshots = get_options_snapshot(atm_symbols)
            for contract in chain["contracts"]:
                sym = contract.get("symbol")
                if sym and sym in snapshots:
                    contract.update(snapshots[sym])

    raw_contracts = chain.get("contracts") or []
    if expiry:
        raw_contracts = [
            c
            for c in raw_contracts
            if not c.get("expiry")
            or _canonical_expiry(str(c.get("expiry", ""))) == expiry
        ]

    # ── Trim chain to 10 contracts max to stay under token limit ──
    trimmed_chain = {
        "underlying":  ticker,
        "spot":        chain.get("spy_price"),
        "favorites_expiry": expiry,
        "dte_days":    dte,
        "contracts":   raw_contracts[:10],
    }

    fav_exp_display = expiry or "unknown — do not ENTER_TRADE"
    trading_constraints = (
        f"- Underlying must be {ticker} only.\n"
        f"- Expiration date must be exactly {fav_exp_display} (YYYY-MM-DD).\n"
        f"- Max premium per 1 contract: ${config.MAX_PREMIUM_PER_TRADE:.0f}.\n"
        "- If action is ENTER_TRADE: pick strike and call/put only from options_chain.contracts; "
        "expiry in JSON must match favorites_expiry exactly."
    )

    # ── Trim signals to essentials only ──
    signals_slim = {
        "time_pst":   signals.get("time_pst"),
        "spy":        signals.get("spy"),
        "vix":        signals.get("vix"),
        "regime":     signals.get("regime"),
        "technicals": {
            "rsi_14":        signals.get("technicals", {}).get("rsi_14"),
            "vwap_dist_pct": signals.get("technicals", {}).get("vwap_dist_pct"),
            "momentum_5bar": signals.get("technicals", {}).get("momentum_5bar"),
        }
    }

    pst = pytz.timezone(config.TIMEZONE)
    now_pst = datetime.now(pst).strftime("%H:%M")

    prompt = ENTRY_DECISION_PROMPT.format(
        time_pst=now_pst,
        morning_thesis=json.dumps(morning_thesis, indent=2),
        signals=json.dumps(signals_slim, indent=2),
        trading_constraints=trading_constraints,
        options_chain=json.dumps(trimmed_chain, indent=2),
        daily_pnl=daily_pnl,
        trades_today=trades_today,
    )

    import time as _time
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=1000,
                system=get_system_prompt(config.PAPER_MODE),
                # No web search — morning scan already has news context
                messages=[{"role": "user", "content": prompt}],
            )

            decision_text = _extract_text(response.content)
            decision = _extract_json(decision_text)

            if not decision:
                decision = {
                    "action": "SKIP",
                    "entry_rationale": f"Could not parse structured decision. Raw: {decision_text[:150]}"
                }

            log_decision(signals, decision_text, decision.get("action", "UNKNOWN"), decision)
            _print_decision(decision)
            return decision

        except Exception as e:
            err = str(e)
            if "429" in err and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"   ⏳ Rate limit hit — waiting {wait}s before retry {attempt+2}/3...")
                _time.sleep(wait)
                continue
            log_error("entry_decision", err)
            print(f"❌ Entry decision failed: {err[:120]}")
            return {"action": "SKIP", "entry_rationale": f"error: {err[:120]}"}

# ──────────────────────────────────────────────────────────
# POSITION MONITOR
# ──────────────────────────────────────────────────────────
def monitor_positions() -> dict:
    """
    Claude reviews open positions and decides: hold or exit.
    Called every 15 min while in a trade.
    """
    positions = get_positions()
    if not positions:
        return {"action": "HOLD", "exit_rationale": "no open positions"}

    signals = get_signal_snapshot()
    pst = pytz.timezone(config.TIMEZONE)
    now_pst = datetime.now(pst).strftime("%H:%M")

    # Force exit check (hard rule, no Claude needed)
    if now_pst >= config.FORCE_EXIT_TIME:
        print(f"⏰ FORCE EXIT: time {now_pst} >= {config.FORCE_EXIT_TIME} PST")
        return {"action": "EXIT_TRADE", "exit_rationale": f"force exit at {now_pst} PST — 0DTE time limit"}

    # Check profit/loss targets (hard rules)
    for pos in positions:
        pnl_pct = pos.get("pnl_pct", 0)
        if pnl_pct >= config.PROFIT_TARGET_PCT:
            return {
                "action": "EXIT_TRADE",
                "exit_rationale": f"profit target hit: {pnl_pct:.1f}% >= {config.PROFIT_TARGET_PCT}%",
                "symbol": pos["symbol"]
            }
        if pnl_pct <= -config.STOP_LOSS_PCT:
            return {
                "action": "EXIT_TRADE",
                "exit_rationale": f"stop loss hit: {pnl_pct:.1f}% <= -{config.STOP_LOSS_PCT}%",
                "symbol": pos["symbol"]
            }

    # Ask Claude for nuanced decision
    prompt = POSITION_MONITOR_PROMPT.format(
        time_pst=now_pst,
        positions=json.dumps(positions, indent=2),
        signals=json.dumps(signals, indent=2),
    )

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=800,
            system=get_system_prompt(config.PAPER_MODE),
            messages=[{"role": "user", "content": prompt}],
        )

        decision_text = _extract_text(response.content)
        decision = _extract_json(decision_text)
        if not decision:
            decision = {"action": "HOLD", "exit_rationale": "parse failed - holding"}

        log_decision(signals, decision_text, decision.get("action", "HOLD"))
        return decision

    except Exception as e:
        log_error("monitor_positions", str(e))
        return {"action": "HOLD", "exit_rationale": f"error - holding: {e}"}

# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────
def _extract_text(content_blocks) -> str:
    """
    Extract final text from Claude response content blocks.
    Takes the LAST text block — that is Claude's final answer,
    not intermediate thinking or search narration.
    """
    last_text = ""
    for block in content_blocks:
        if hasattr(block, "text") and block.text and block.text.strip():
            last_text = block.text.strip()
    return last_text

def _extract_json(text: str) -> dict:
    """
    Robustly extract JSON from Claude's response.
    Handles: raw JSON, ```json blocks, JSON embedded in prose,
    and JSON fields returned without outer braces.
    """
    if not text:
        return {}

    # 1. Strip markdown code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()

    # 2. Try direct parse
    try:
        return json.loads(cleaned)
    except:
        pass

    # 3. Find outermost { } block in original text
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except:
            pass

    # 4. Claude returned fields without outer braces — wrap and try
    # e.g. \'bias\': \'neutral\', \'thesis\': \'...\' 
    try:
        wrapped = "{" + cleaned.rstrip(",") + "}"
        return json.loads(wrapped)
    except:
        pass

    # 5. Fix common issues: trailing commas, single quotes
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", cleaned)
        fixed = fixed.replace("'", '"')
        return json.loads(fixed)
    except:
        pass

    # 6. Same fixes + wrap in braces
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", cleaned)
        fixed = fixed.replace("'", '"')
        return json.loads("{" + fixed.rstrip(",") + "}")
    except:
        pass

    return {}

def _detect_bias(text: str) -> str:
    """Fallback: detect bias from plain text response"""
    text_lower = text.lower()
    bullish_words = ["bullish", "upside", "rally", "positive", "strong", "buy"]
    bearish_words = ["bearish", "downside", "sell-off", "negative", "weak", "caution"]
    bull_score = sum(1 for w in bullish_words if w in text_lower)
    bear_score = sum(1 for w in bearish_words if w in text_lower)
    if bull_score > bear_score:   return "bullish"
    elif bear_score > bull_score: return "bearish"
    return "neutral"

def _print_decision(decision: dict):
    action = decision.get("action", "?")
    emoji  = {"ENTER_TRADE": "🟢", "SKIP": "⏭️", "HALT_TRADING": "🛑",
              "EXIT_TRADE":  "🔴", "HOLD": "⏸️"}.get(action, "❓")
    print(f"\n{emoji} ACTION: {action}")
    if action == "ENTER_TRADE":
        print(f"   Strategy:   {decision.get('strategy')}")
        print(f"   Direction:  {str(decision.get('direction','?')).upper()}")
        print(f"   Strike:     ${decision.get('strike')}")
        print(f"   Expiry:     {decision.get('expiry')}")
        print(f"   Confidence: {decision.get('confidence', 0):.0%}")
        print(f"   Risk:       {decision.get('risk_assessment')}")
    rationale = decision.get("entry_rationale") or decision.get("exit_rationale") or ""
    print(f"   Rationale:  {rationale[:120]}")
    if decision.get("warnings"):
        for w in decision["warnings"]:
            print(f"   ⚠️  {w}")
    print()
