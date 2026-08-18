from kalshi_btc_bot.model import DistModel
from kalshi_btc_bot.signals import SignalEngine


class FixedDist(DistModel):
    def __init__(self, p):
        self.p = p

    def true_prob(self, *_args, **_kwargs):
        return self.p


def test_posterior_falls_back_without_market_quote():
    info = FixedDist(0.62).posterior_prob({}, 1, 1, 1, {})
    assert info["true_prob"] == 0.62
    assert info["market_prob"] is None
    assert info["market_weight"] == 0.0


def test_explicit_market_mid_is_used_as_evidence():
    info = FixedDist(0.70).posterior_prob(
        {}, 1, 1, 1, {}, bid=0.20, ask=0.30, market_mid=0.40
    )
    assert info["market_prob"] == 0.40
    assert 0.40 < info["true_prob"] < 0.70


def test_bid_ask_mid_moves_posterior_toward_market():
    info = FixedDist(0.70).posterior_prob({}, 1, 1, 1, {}, bid=0.20, ask=0.30)
    assert info["market_prob"] == 0.25
    assert 0.25 < info["true_prob"] < 0.70


def test_tighter_spread_gets_more_market_weight():
    tight = FixedDist(0.70).posterior_prob({}, 1, 1, 1, {}, bid=0.29, ask=0.31)
    wide = FixedDist(0.70).posterior_prob({}, 1, 1, 1, {}, bid=0.20, ask=0.40)
    assert tight["market_weight"] > wide["market_weight"]
    assert tight["true_prob"] < wide["true_prob"]


def test_signal_engine_can_ignore_market_quote_for_synthetic_backtest():
    engine = SignalEngine(FixedDist(0.70), use_market_posterior=False)
    info = engine._posterior({"hours": 1.0, "bid": 0.20, "ask": 0.30}, 1, 1, {})
    assert info["prior_prob"] == 0.70
    assert info["true_prob"] == 0.70
    assert info["market_prob"] is None
    assert info["market_weight"] == 0.0
