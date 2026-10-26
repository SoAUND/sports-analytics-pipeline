"""Durable ET research cadence and delivery outbox. No sportsbook access."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import json
import sqlite3
from .revised_strategy import ET, timestamp, evaluate_research, research_card, result_card, pick_line, et_time, american, literal_pick, SPORTS
from .deep_analysis import audit_candidate
from .full_evaluation import evaluation_lines


def level30_locked(row):
    """Tip-30 covers Tier 1-3. A probable role is confirmed here, not left off the schedule."""
    return row.get('research_tier') in {'tier1', 'tier2', 'tier3'} or row.get('publication_decision') in {'official_pick', 'conditional_pick'}


def real_selection(row):
    """A row is a selection only when event, market, and side are all identified."""
    return bool(row.get("event_id") and row.get("market") and (row.get("selection_name") or row.get("participant")))


def card_selection(row):
    """Show a real selection even when it stopped short of complete strategy.

    Level 1 discovery rows without that identity are not selections and stay off
    the picks list so they cannot be dressed up as Official.
    """
    if not real_selection(row):
        return False
    if row.get("publication_decision") not in {
            "official_pick", "experimental", "conditional_pick", "watchlist",
            "experimental_call", "test_only_selection"}:
        return False
    return audit_candidate(row).level > 1


def shortfall_label(row):
    """Name the level reached and the first gate or evidence gap. Do not invent a metric."""
    audit = audit_candidate(row)
    decision = str(row.get("publication_decision") or "unlabeled").replace("_", " ")
    if row.get("publication_decision") == "official_pick" and audit.level30_complete:
        return "Official"
    if row.get('full_evaluation'):
        return 'Provisional '+decision+' | Full 7-stage / 12-qualification audit recorded'
    gap = (audit.missing_evidence[:1] or audit.missing_metrics[:1] or audit.missing_output[:1] or [None])[0]
    gate = audit.first_blocker or row.get("first_blocker")
    parts = [f"Stage {audit.level} {decision}"]
    if gate:
        parts.append(str(gate))
    if gap and str(gap) != str(gate):
        parts.append(str(gap))
    return " | ".join(parts)


def _existing_edge(row):
    """Use an edge only when evaluation already recorded one. Never calculate a new one."""
    value = row.get("probability_edge")
    if value in (None, ""):
        return None
    try:
        edge = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not edge.is_finite():
        return None
    return edge


def display_rank(row):
    """Order completed official audits first, then deeper audits, then an existing edge.

    Rows with no recorded edge keep start-time order. reference quotes are not treated as edge.
    """
    audit = audit_candidate(row)
    official = 0 if audit.level30_complete and row.get("publication_decision") == "official_pick" else 1
    edge = _existing_edge(row)
    return (official, -audit.level, 0 if edge is not None else 1,
            -edge if edge is not None else Decimal(0),
            str(row.get("start_time") or ""), str(row.get("event_id") or ""))


TIER_HEADINGS = (
    "🔥 TIER 1 — STRONGEST PICKS",
    "✅ TIER 2 — STANDARD PICKS",
    "⚠️ TIER 3 — CONDITIONAL PICKS",
    "🧪 TEST-ONLY — EXPERIMENTAL",
    "👀 WATCHLIST — NOT PICKS",
    "⛔ PASSES",
    "📭 MISSING / UNAVAILABLE",
)


def report_tier(row):
    """Research-first tier. Price and model validation do not demote a sourced grade."""
    if row.get('publication_decision') in {'watchlist', 'pass'}:
        return 4 if row.get('publication_decision') == 'watchlist' else 5
    tier = row.get('research_tier')
    if tier == 'tier1':
        return 0
    if tier == 'tier2':
        return 1
    if tier == 'tier3':
        return 2
    if tier == 'test' or row.get('publication_decision') in {'experimental', 'test_only_selection'}:
        return 3
    if tier == 'watchlist' or row.get('publication_decision') in {'watchlist', 'experimental_call'}:
        return 4
    return 6 if row.get('market_status') in {'unavailable', 'missing', 'suspended'} else 5


def best_available(rows, *, as_of, limit=3):
    """Rank current daily choices without pretending incomplete research is verified EV."""
    eligible=[]
    for row in rows:
        try:
            if not real_selection(row) or row.get('market_status') in {'missing','unavailable','suspended','closed'}:continue
            tip=timestamp(row['start_time']);quote=timestamp(row['price_time'])
            if tip<=as_of or tip.astimezone(ET).date()!=as_of.astimezone(ET).date():continue
            if not as_of-timedelta(minutes=60)<=quote<=as_of:continue
            price=Decimal(str(row['decimal_odds']))
            if not price.is_finite() or price<=1:continue
            edge=_existing_edge(row)
            eligible.append(row)
        except (KeyError,ValueError,TypeError,InvalidOperation):continue
    def rank(r):
        order = {'tier1': 0, 'tier2': 1, 'tier3': 2, 'test': 3}.get(r.get('research_tier'), 4)
        return (order, -int(r.get('research_score') or 0), str(r.get('start_time')), str(r.get('event_id')))
    unique={}
    for row in sorted(eligible,key=rank):
        unique.setdefault(row['event_id'],row)
    return list(unique.values())[:limit]


def delivery_cards(rows, *, title, as_of, parlay_result=None, include_details=True):
    """One tiered summary and grouped evidence cards, rather than a card per row."""
    rows = list(rows)
    shown = sorted((r for r in rows if card_selection(r)), key=display_rank)
    groups = {i: [] for i in range(len(TIER_HEADINGS))}
    for row in shown:
        groups[report_tier(row)].append(row)
    for row in rows:
        if row.get("publication_decision") == "pass" or row.get("market_status") in {"unavailable", "missing", "suspended"}:
            if row not in shown:
                groups[report_tier(row)].append(row)
    official = bool(groups[0] or groups[1])
    summary = ["PICKS — " + title, "🕒 Checked: " + et_time(as_of.isoformat()), ""]
    if not official:
        summary.extend(["No official picks today.", ""])
    daily=best_available(rows,as_of=as_of)
    summary.extend(['🏅 BEST OF THE DAY — AVAILABLE SELECTIONS',
        'Ranked by research tier, then Research Strength Score. Price is bonus only.', ''])
    for index,row in enumerate(daily,1):
        summary.append(f'{index}. '+pick_line(row))
        summary.append('Strength: '+str(row.get('confidence','Ungraded'))+' | '+shortfall_label(row))
        summary.append('📍 '+str(row.get('book_of_record','Public reference'))+' | Books: '+str(row.get('matching_book_count','N/A')))
        edge=_existing_edge(row)
        summary.append(f'Edge: {edge*100:.2f} pp' if edge is not None else 'Edge: unverified — market-consensus selection')
        summary.append('⚠️ '+display_risk(row))
        summary.extend(evaluation_lines(row))
        follow=row.get('follow_through')
        if follow:
            summary.append('Follow-through: '+str(follow.get('status'))+' | added '+', '.join(follow.get('added') or ['none'])+' | still missing '+', '.join(follow.get('still_missing') or ['none']))
        summary.append('')
    if not daily:summary.extend(['No current, priced, unstarted selections available. Refresh the board.', ''])

    for tier, heading in enumerate(TIER_HEADINGS):
        items = groups[tier]
        if not items:
            continue
        summary.extend([heading + f" ({len(items)})", ""])
        if tier == 4:
            summary.extend([f"{len(items)} research-only entries retained in the run audit; not betting selections.", ""])
            continue
        if tier >= 5:
            reasons = {}
            for row in items:
                reason = str(row.get("first_blocker") or "Evidence incomplete")
                reasons[reason] = reasons.get(reason, 0) + 1
            summary.extend(f"• {count} selection(s): {reason}" for reason, count in sorted(reasons.items()))
            summary.append("")
            continue
        for index, row in enumerate(items[:3], 1):
            summary.append(f"{index}. " + pick_line(row))
            summary.append("Strength: " + str(row.get("confidence", "Ungraded")) + " | Score: " + str(row.get("research_score", row.get("score", "N/A"))))
            edge = _existing_edge(row)
            if edge is not None:
                summary.append(f"Edge: {edge * 100:.2f} pp | EV/$: " + str(row.get("ev_per_dollar", "N/A")))
            if tier >= 2:
                summary.append("Label: " + shortfall_label(row))
            if row.get("book_of_record"):
                summary.append("📍 Price source: " + str(row["book_of_record"]))
            if row.get("price_scope") == "public_reference":
                summary.append("⚠️ Primary-book price unverified | Minimum decimal price: " + str(row.get("minimum_decimal_odds", "N/A")))
            summary.extend(evaluation_lines(row))
            summary.append("")
        if len(items) > 3:
            summary.extend([f"{len(items)-3} additional entries retained in the run report.", ""])
    summary.append("🏆 Highest Confidence Only: " + (str(len(groups[0])) + " in Tier 1" if groups[0] else "none"))
    summary.extend(["", "Research only. Verify price at your book before acting."])
    parlay_detail = research_card(rows, title="PARLAY REVIEW", as_of=as_of)
    parlay_section = parlay_detail.split("PLUS-MONEY PARLAYS", 1)[1].split("Public research:", 1)[0]
    if parlay_result is None:
        summary.extend(["", "🎟️ PLUS-MONEY PARLAYS", parlay_section])
    else:
        from .parlay_builder import parlay_report
        summary = summary[:3] + [parlay_report(parlay_result), ""] + summary[3:]
    cards = ["\n".join(summary)]
    if not include_details:
        return cards
    # Preserve full evidence in grouped cards. Incomplete discovery stays out of picks.
    detail_groups = {i: [] for i in range(len(TIER_HEADINGS))}
    for row in rows:
        tier = report_tier(row)
        header = "RESEARCH DETAILS | " + str(row.get("sport", "")).upper() + " | " + str(row.get("away_name", "Away")) + " vs " + str(row.get("home_name", "Home")) + " | Selection: " + str(row.get("selection_name", "Watch"))
        detail = research_card([row], title=header, as_of=as_of)
        # Ticket review is published once on the summary, never repeated per leg.
        before, _, after = detail.partition("PLUS-MONEY PARLAYS")
        detail = before.rstrip()
        if "Public research:" in after:
            detail += "\nPublic research:" + after.split("Public research:", 1)[1]
        detail = "\n".join(line for line in detail.splitlines() if line != "No official picks today.")
        detail_groups[tier].append(detail)
    for tier, details in detail_groups.items():
        if details:
            cards.append("RESEARCH DETAILS — " + TIER_HEADINGS[tier] + "\n\n" + "\n\n".join(details))
    return cards


def display_risk(row):
    """Translate legacy missing-key errors without changing eligibility decisions."""
    value=str(row.get('largest_risk') or row.get('first_blocker') or 'Research incomplete')
    if not row.get('model_probability') and not (row.get('model') or {}).get('probability'):
        return 'Independent probability model missing; edge and expected value unverified'
    if value.strip("'\"")=='model' or value=="Candidate preparation failed: 'model'":
        return 'Independent probability model missing; edge and expected value unverified'
    return value


def initialize(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS research_jobs (
      job_key TEXT PRIMARY KEY, kind TEXT NOT NULL, due_at TEXT NOT NULL,
      payload_json TEXT NOT NULL, completed_at TEXT);
    CREATE TABLE IF NOT EXISTS research_outbox (
      message_key TEXT PRIMARY KEY, message TEXT NOT NULL, created_at TEXT NOT NULL,
      delivered_at TEXT);
    CREATE TABLE IF NOT EXISTS research_audit (
      run_at TEXT NOT NULL, input_key TEXT NOT NULL, decision_json TEXT NOT NULL,
      PRIMARY KEY(run_at,input_key));
    """)


def due_jobs(connection, now):
    initialize(connection)
    return connection.execute("SELECT * FROM research_jobs WHERE completed_at IS NULL AND due_at<=? ORDER BY due_at,job_key", (now.isoformat(),)).fetchall()


def prepare_daily_jobs(connection, now):
    initialize(connection)
    local=now.astimezone(ET)
    # Each daily job exists once, including across cron delays and DST changes.
    for kind,hour,minute in [("overnight",0,0),("midday",10,0)]:
        key=f"{local.date()}:{kind}"
        due=local.replace(hour=hour,minute=minute,second=0,microsecond=0).astimezone(now.tzinfo)
        connection.execute("INSERT OR IGNORE INTO research_jobs VALUES(?,?,?,?,NULL)",(key,kind,due.isoformat(),"{}"))
        connection.execute("UPDATE research_jobs SET due_at=? WHERE job_key=? AND completed_at IS NULL",(due.isoformat(),key))
    connection.commit()


def prepare_manual_job(connection, now):
    """A fresh on-demand review has its own identity and preserves daily jobs."""
    initialize(connection)
    key="manual:"+now.isoformat()
    connection.execute("INSERT OR IGNORE INTO research_jobs VALUES(?,?,?,?,NULL)",
                       (key,"manual",now.isoformat(),"{}"))
    connection.commit()
    return key


def run_due(connection, now, payload):
    """Fresh input is required at every tip check; missing/changed legs are scratched.

    Outbox insert and job completion commit atomically. Completed one-shot checks
    never wake again; retain their audit rows instead of deleting the evidence.
    """
    initialize(connection)
    candidates=payload.get("candidates",[])
    evaluated=[evaluate_research(r,as_of=now) for r in candidates]
    calls=[]
    for original in payload.get("experimental_calls",[]):
        checked=evaluate_research(original,as_of=now)
        # Market-only calls remain explicitly provisional, never inherit a grade.
        checked.update(publication_decision=original.get('publication_decision','watchlist'),
                       confidence=original.get('confidence','Ungraded'))
        from .full_evaluation import full_evaluation
        checked['full_evaluation']=full_evaluation(checked,as_of=now)
        calls.append(checked)
    qualified_ids={r.get("event_id") for r in evaluated if r.get("publication_decision") in {"official_pick","conditional_pick","experimental"}}
    calls=[r for r in calls if r.get("event_id") not in qualified_ids]
    call_ids={r.get("event_id") for r in calls}
    card_rows=[r for r in evaluated if r.get("event_id") not in call_ids]+calls
    from .parlay_builder import build_parlays
    ticket_quotes = list(payload.get('parlay_tickets', []))
    for row in evaluated:
        ticket_quotes.extend(row.get('parlay_tickets', []))
    parlay_result = build_parlays(evaluated, as_of=now, ticket_quotes=ticket_quotes)
    connection.execute('CREATE TABLE IF NOT EXISTS research_parlay_audit (run_at TEXT PRIMARY KEY, result_json TEXT NOT NULL)')
    connection.execute('INSERT OR REPLACE INTO research_parlay_audit VALUES (?,?)',
                       (now.isoformat(), json.dumps(parlay_result, default=str)))
    lookup={str(r.get("candidate_id",r.get("event_id"))):r for r in evaluated}
    for job in due_jobs(connection,now):
        key,kind=job["job_key"],job["kind"]
        with connection:
            if kind=="manual":
                cards=delivery_cards(card_rows,title="LIVE RESEARCH CHECK",as_of=now,parlay_result=parlay_result,include_details=False)
                msg=cards[0]
            elif kind=="overnight":
                slate=payload.get("slate_events",candidates+calls)
                unique={str(r.get("event_id")):r for r in slate if r.get("event_id") and r.get("start_time") and timestamp(r["start_time"])>now and timestamp(r["start_time"]).astimezone(ET).date()==now.astimezone(ET).date()}
                msg="MIDNIGHT SLATE LOOK\nChecked: "+et_time(now.isoformat())+"\nScheduled: "+et_time(job["due_at"])+"\n\nNo picks posted.\n\n"+"\n\n".join(
                    pick_line({**r,"selection_name":"Slate watch","market":"watch","decimal_odds":None,"american_odds":""}).split(" | Slate watch")[0]
                    for r in sorted(unique.values(),key=lambda r:(r["start_time"],r.get("sport",""))))
                coverage=payload.get("slate_coverage")
                if coverage:
                    msg+="\n\nCOVERAGE: "+str(coverage.get("total_events",0))+" remaining events; "+str(coverage.get("active_leagues_checked",0))+" active provider leagues checked.\n"+coverage["scope"]
                    failures=[r["provider_sport"]+": "+r["status"] for r in coverage["leagues"] if r["status"]=="Provider request failed"]
                    if coverage.get("error"):failures.append(coverage["error"])
                    if failures:msg+="\nIncomplete discovery: "+"; ".join(failures)
            elif kind=="midday":
                # Delayed wakes still evaluate fresh evidence at the actual run time.
                title="MIDDAY CHECK — DELAYED" if now-timestamp(job["due_at"])>timedelta(minutes=15) else "MIDDAY LOCK"
                cards=delivery_cards(card_rows,title=title,as_of=now,parlay_result=parlay_result,include_details=False)
                msg=cards[0]
                by_tip={}
                for r in evaluated:
                    if level30_locked(r):
                        by_tip.setdefault(r["start_time"],[]).append(r)
                for tip,rows in by_tip.items():
                    due=timestamp(tip)-timedelta(minutes=30)
                    if due<now:
                        msg+="\n\nTip-30 already passed for "+tip+"; immediate re-check required."
                        continue
                    tip_key=key.split(":")[0]+":tip30:"+tip
                    connection.execute("INSERT OR IGNORE INTO research_jobs VALUES(?,?,?,?,NULL)",
                        (tip_key,"tip30",due.isoformat(),json.dumps(rows)))
            else:
                locked=json.loads(job["payload_json"]);lines=["TIP-30 RE-CHECK",""]
                for old in locked:
                    candidate=lookup.get(str(old.get("candidate_id",old.get("event_id"))))
                    identical = candidate and all(candidate.get(k)==old.get(k) for k in ("event_id","market","period","line","participant","selection_name","settlement_rule","book_of_record"))
                    if not identical or not level30_locked(candidate):
                        reason=candidate.get("first_blocker") if candidate else "Fresh evidence missing"
                        lines.append("SCRATCH | "+pick_line(old)+" | "+str(reason or "Market changed; new leg needs review"))
                    elif candidate.get("decimal_odds")!=old.get("decimal_odds"):
                        lines.append("TWEAK | "+pick_line(candidate)+" | Recalculated current price; "+candidate["honesty_label"])
                    else:
                        lines.append("CONFIRM | "+pick_line(candidate)+" | "+candidate["honesty_label"])
                msg="\n".join(lines)
            connection.execute("INSERT OR IGNORE INTO research_outbox VALUES(?,?,?,NULL)",(key,msg,now.isoformat()))
            if kind in {"manual","midday"} and payload.get("slate_coverage"):
                coverage=payload["slate_coverage"]
                lines=["SLATE COVERAGE",coverage["scope"],str(coverage.get("total_events",0))+" remaining events discovered.",coverage["additional_markets"],""]
                if coverage.get("error"):lines.append(coverage["error"])
                lines.append(str(coverage.get('active_leagues_checked',0))+" active provider leagues checked.")
                failures=[row for row in coverage['leagues'] if row['status'] in {'Provider request failed','Book configuration unavailable'} or row['status'].startswith('Odds deferred')]
                lines.append(str(len(failures))+" leagues with failed or deferred odds checks.")
                lines.append("Full event board, league statuses and missing markets are retained in the run's slate-coverage report. Discovery is not a pick.")
                connection.execute("INSERT OR IGNORE INTO research_outbox VALUES(?,?,?,NULL)",(key+":coverage","\n".join(lines),now.isoformat()))
            if kind in {"manual","midday"}:
                for index,detail in enumerate(cards[1:],1):
                    connection.execute("INSERT OR IGNORE INTO research_outbox VALUES(?,?,?,NULL)",
                                       (key+f":details:{index:03}",detail,now.isoformat()))
            connection.execute("UPDATE research_jobs SET completed_at=? WHERE job_key=?",(now.isoformat(),key))
    for i,row in enumerate(evaluated+calls):
        connection.execute("INSERT OR IGNORE INTO research_audit VALUES(?,?,?)",(now.isoformat(),str(row.get("candidate_id",i)),json.dumps(row,default=str)))
    # Results use the actual placed-ticket payload, not the earlier proposed slip.
    for ticket in payload.get("settled_tickets",[]):
        if ticket.get("ticket_id") and ticket.get("legs"):
            msg=result_card(ticket["legs"])
            connection.execute("INSERT OR IGNORE INTO research_outbox VALUES(?,?,?,NULL)",("result:"+str(ticket["ticket_id"]),msg,now.isoformat()))
    connection.commit()
    return evaluated


def pending_messages(connection):
    initialize(connection)
    return connection.execute("SELECT * FROM research_outbox WHERE delivered_at IS NULL ORDER BY created_at, CASE WHEN message LIKE 'PICKS%' THEN 1 ELSE 0 END, message_key").fetchall()


def mark_delivered(connection,key,now):
    with connection:
        connection.execute("UPDATE research_outbox SET delivered_at=? WHERE message_key=?",(now.isoformat(),key))
