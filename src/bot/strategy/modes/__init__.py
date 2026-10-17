"""Pure rule core of the LONG module: configuration, mode selector and the mode rules.

Nothing here touches the venue, the network or the live engine, and no live module imports it:
the offline backtest in ``research.mode_backtest`` calls these rules, and a later phase wires the
same rules into the engine. The rules take numbers, never bar arrays, so none of them imports
``bot.strategy.pine_indicators``; ``config`` reads the one YAML file and nothing else.
"""
