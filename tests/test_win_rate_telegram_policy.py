from scripts.enforce_win_rate_telegram_policy import enforce_policy, odds_in_win_rate_range


def test_odds_range_edges():
    assert odds_in_win_rate_range(-400) is True
    assert odds_in_win_rate_range(300) is True
    assert odds_in_win_rate_range(-401) is False
    assert odds_in_win_rate_range(301) is False


def test_minus_526_is_never_called_bot_lean():
    text = "\n".join(
        [
            "Status: TEST CAPTURED",
            "",
            "🚨 BOT LEAN: ALEXANDER ZVEREV ML",
            "Book: pinnacle",
            "Odds: -526",
            "Qualified Selection: NO",
        ]
    )
    result = enforce_policy(text, accessible_bookmakers={"hardrockbet"})
    assert "BOT LEAN:" not in result
    assert "⛔ OUTSIDE WIN-RATE RANGE: ALEXANDER ZVEREV ML" in result
    assert "Status: PASS — OUTSIDE WIN-RATE RANGE" in result
    assert "Reference Book: pinnacle" in result
    assert "Win-Rate Lane: PASS — approved individual price range is -400 through +300" in result


def test_accessible_book_is_labeled_accessible():
    text = "\n".join(
        [
            "Status: READY",
            "🚨 BOT LEAN: ARSENAL ML",
            "Book: hardrockbet",
            "Odds: -145",
        ]
    )
    result = enforce_policy(text, accessible_bookmakers={"hardrockbet"})
    assert "🚨 BOT LEAN: ARSENAL ML" in result
    assert "Accessible Book: hardrockbet" in result
    assert "OUTSIDE WIN-RATE RANGE" not in result


def test_reference_book_inside_range_can_remain_test_lean_but_is_not_called_accessible():
    text = "\n".join(
        [
            "Status: TEST CAPTURED",
            "🚨 BOT LEAN: BOTIC VAN DE ZANDSCHULP ML",
            "Book: pinnacle",
            "Odds: -357",
        ]
    )
    result = enforce_policy(text, accessible_bookmakers={"hardrockbet"})
    assert "🚨 BOT LEAN: BOTIC VAN DE ZANDSCHULP ML" in result
    assert "Reference Book: pinnacle" in result
    assert "Accessible Book: pinnacle" not in result
