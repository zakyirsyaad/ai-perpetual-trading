"""Pure rule core of the LONG module: configuration, mode selector and the mode rules.

Nothing here touches the venue, the network or the live engine, and exactly one live module
imports it: ``bot/core/regime_gate.py`` (the LONG forward bot's regime gate, off by default). The
offline backtest in ``research.mode_backtest`` calls these rules as well. The rules take numbers, never bar arrays, so none of them imports
``bot.strategy.pine_indicators``; ``config`` reads the one YAML file and nothing else.
``near_mean`` takes one short window of highs, lows and closes and measures it with the live bot's
own ``bot.strategy.indicators`` and setup profile, so its stretch is the number the live gate gets.
``position_rules`` holds the bracket and slippage arithmetic and the entry gates that every mode
shares, and reads the weekly window through the live ``bot.core.entry_window``. ``checks`` is the
one copy of the number and side checks that the rules and the backtest share.
"""
