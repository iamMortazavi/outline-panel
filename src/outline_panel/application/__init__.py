"""Use cases: load the aggregate, ask it what should happen, make it happen.

Nothing here decides policy — that is the domain's job — and nothing here knows
about HTTP. The dashboard, the Telegram bot, the Mini App and the scheduler all
enter through these, which is what stops a third implementation of "extend"
appearing the way it did in `bot/dispatcher.py`.
"""
