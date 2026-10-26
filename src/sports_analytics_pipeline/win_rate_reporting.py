"""Reporting helpers for the test-only Win-Rate-First strategy lane."""

from __future__ import annotations

from decimal import Decimal
import json
from typing import Any, Mapping

from .win_rate_strategy import (
    D,
    MIN_TICKET_DECIMAL_ODDS,
    WinRateCandidate,
    build_win_rate_parlays,
    decimal_to_american,
    win_rate_display_fields,
)

SPORT_LABELS = {"nba": "NBA", "wnba": "WNBA", "epl": "EPL", "mma": "MMA/UFC", "atp": "ATP", "wta": "WTA"}


def _dec(value: Any, scale: Decimal = D("1")) -> Decimal:
    try:
        return D(str(value)) / scale
    except Exception:
        return D("0")


def _selection_name(selection: Mapping[str, Any]) -> str:
    name = selection.get("selection_name")
    return str(name) if name else str(selection.get("outcome_role") or "selection").replace("_", " ").title()


def _market(selection: Mapping[str, Any]) -> str:
    return str(selection.get("market_key") or "market").replace("_", " ").title()


def _price_quality_label(value: str) -> str:
    return {"strong_value": "Strong Value", "positive_value": "Positive Value", "slightly_expensive": "Slightly Expensive", "expensive": "Expensive"}.get(value, value.replace("_", " ").title())


def _probability_band_label(value: str) -> str:
    return {"premium": "Premium", "strong": "Strong", "selective": "Selective", "below_lane": "Below Lane"}.get(value, value.replace("_", " ").title())


def _lane_status_label(value: str) -> str:
    return {"test_candidate": "TEST CANDIDATE", "watch_only": "WATCH ONLY — SAFEGUARD BLOCKED", "pass": "PASS"}.get(value, value.replace("_", " ").upper())


def _ev_lane_status(selection: Mapping[str, Any]) -> str:
    decision_type = str(selection.get("decision_type") or "pass")
    confidence = str(selection.get("confidence_label") or "")
    if confidence == "Unqualified Test Observation":
        return "PASS / UNQUALIFIED TEST CAPTURE"
    return {"official_pick": "QUALIFIED", "conditional_pick": "CONDITIONAL", "test_only_selection": "TEST-ONLY QUALIFIED"}.get(decision_type, "PASS")


def _gate_results(raw_decision: Mapping[str, Any] | None) -> dict[str, bool]:
    if not raw_decision:
        return {}
    value = raw_decision.get("gate_results_json")
    if isinstance(value, Mapping):
        return {str(k): bool(v) for k, v in value.items()}
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, Mapping):
            return {str(k): bool(v) for k, v in parsed.items()}
    return {}


def _research_complete(selection: Mapping[str, Any], raw_decision: Mapping[str, Any] | None = None) -> bool:
    gates = _gate_results(raw_decision)
    if gates:
        return all(gates.get(k) is True for k in ("research", "material_information", "conflict_and_integrity"))
    return str(selection.get("confidence_label") or "") != "Unqualified Test Observation"


def classify_selection(selection: Mapping[str, Any], raw_decision: Mapping[str, Any] | None = None) -> dict[str, Any]:
    decimal_odds = _dec(selection.get("decimal_odds"))
    conservative = _dec(selection.get("conservative_probability_percent"), D("100"))
    edge = _dec(selection.get("edge_percentage_points"), D("100"))
    ev = _dec(selection.get("expected_value_percent"), D("100"))
    research_complete = _research_complete(selection, raw_decision)
    lane = win_rate_display_fields(decimal_odds=decimal_odds, conservative_probability=conservative, probability_edge=edge, ev=ev, research_complete=research_complete, stability_clear=research_complete)
    return {**lane, "decimal_odds": decimal_odds, "conservative_probability": conservative, "probability_edge": edge, "ev": ev, "american_odds": decimal_to_american(decimal_odds) if decimal_odds > 1 else None, "ev_lane_status": _ev_lane_status(selection)}


def _current_selections(status: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any], Mapping[str, Any] | None, Mapping[str, Any]]]:
    sports = status.get("sports")
    if not isinstance(sports, Mapping):
        return []
    rows = []
    for sport, payload in sports.items():
        if not isinstance(payload, Mapping):
            continue
        selection = payload.get("best_current_paper_selection_display")
        raw = payload.get("best_current_paper_selection")
        raw = raw if isinstance(raw, Mapping) else None
        if isinstance(selection, Mapping) and selection.get("decision_type") != "pass":
            rows.append((str(sport), selection, raw, payload))
    return rows


def _candidate_for_parlay(sport: str, selection: Mapping[str, Any], classification: Mapping[str, Any]) -> WinRateCandidate | None:
    if classification.get("status") != "test_candidate":
        return None
    event_id = str(selection.get("event_id") or "")
    return WinRateCandidate(
        key=f"{sport.upper()}: {_selection_name(selection)}",
        market=str(selection.get("market_key") or "unknown"),
        decimal_odds=D(str(classification["decimal_odds"])),
        conservative_probability=D(str(classification["conservative_probability"])),
        break_even_probability=D("1") / D(str(classification["decimal_odds"])),
        ev=D(str(classification["ev"])),
        probability_edge=D(str(classification["probability_edge"])),
        information_quality="complete_current",
        stability_status="stable",
        source={"event_id": event_id, "sport": sport},
    )


def build_win_rate_report(status: Mapping[str, Any]) -> str:
    rows = _current_selections(status)
    lines = [
        "Mode: test_only",
        "Strategy: Win-Rate-First v1.1",
        "Eligible individual price range: -400 through +300",
        "Minimum conservative win probability: 55%",
        "Minimum ticket return: 4.00x total return (+300 combined odds)",
        "Ticket rule: use the fewest strong legs that reach 4x; never force a ticket.",
        "",
        "Current Win-Rate Candidates:",
    ]
    parlay_candidates: list[WinRateCandidate] = []
    if not rows:
        lines.append("None in the current pregame window.")
    for sport, selection, raw_decision, payload in rows:
        classified = classify_selection(selection, raw_decision)
        odds = classified.get("american_odds")
        odds_text = f"+{odds}" if isinstance(odds, int) and odds > 0 else str(odds)
        lines.extend([
            "",
            f"{SPORT_LABELS.get(sport, sport.upper())} — {_selection_name(selection)}",
            f"Market: {_market(selection)}",
            f"Odds: {odds_text}",
            f"Win Probability: {float(classified['conservative_probability'] * D('100')):.2f}% — {_probability_band_label(str(classified['probability_band']))}",
            f"Price Quality: {_price_quality_label(str(classified['price_quality']))} | Edge {float(classified['probability_edge'] * D('100')):+.2f} pp | EV {float(classified['ev'] * D('100')):+.2f}%",
            f"Research Safeguard: {'CLEAR' if classified['research_safeguard_clear'] else 'BLOCKED'}",
            f"Win-Rate Lane: {_lane_status_label(str(classified['status']))}",
            f"EV Lane: {classified['ev_lane_status']}",
            f"Model Maturity: {payload.get('historical_training_examples', 0)}/{payload.get('training_minimum', 50)} training | {payload.get('completed_oos', 0)}/{payload.get('validation_target', 100)} OOS | {str(payload.get('model_status') or 'unknown').replace('_', ' ').upper()}",
        ])
        candidate = _candidate_for_parlay(sport, selection, classified)
        if candidate is not None:
            parlay_candidates.append(candidate)

    cards = build_win_rate_parlays(parlay_candidates)
    lines.extend(["", "Minimum-4x High-Hit-Rate Ticket:"])
    if not cards:
        lines.append("NO 4X TICKET — no safeguard-cleared 2-4 leg combination reaches 4.00 decimal without forcing weaker legs.")
    else:
        card = cards[0]
        odds = int(card["american_odds"])
        odds_text = f"+{odds}" if odds > 0 else str(odds)
        joint = D(str(card["joint_probability"])) * D("100")
        ev = D(str(card["ev"])) * D("100")
        multiple = D(str(card["return_multiple"]))
        lines.extend([
            f"{card['leg_count']}-Leg: {' + '.join(card['legs'])}",
            f"Combined Odds: {odds_text} | Decimal: {multiple:.2f}",
            f"Total Return Multiple: {multiple:.2f}x | Minimum Required: {MIN_TICKET_DECIMAL_ODDS:.2f}x",
            f"Estimated Joint Win: {float(joint):.2f}% | EV: {float(ev):+.2f}%",
            f"$10 Test Risk → Total Return ${float(multiple * D('10')):.2f} | Profit ${float((multiple - D('1')) * D('10')):.2f}",
        ])

    lines.extend([
        "",
        "Correlation rule: same-event legs are excluded unless a validated joint model exists.",
        "Joint probability uses an independence approximation for different events.",
        "The existing expected-value qualification lane remains unchanged.",
        "TEST ONLY — No real-money action is taken.",
    ])
    return "\n".join(lines)
