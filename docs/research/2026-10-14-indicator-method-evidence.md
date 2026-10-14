# Indicators and methods for the Pine-adapted gate — evidence review

Date: 2026-10-14 · About 70 sources from three research streams, plus our own measurements ·
Confidence: medium (peer-reviewed intraday crypto work is thin and mostly pre-2022; several 2026
preprints are single-source and marked as such).

Question asked: are there better or more powerful indicators or methods than EMA trend + RSI
pullback + ADX/DI + volume-profile POC for BTC/ETH perpetuals, 5-minute signals, a symmetric
±0.20% bracket (10–30 minute resolution) and a target WR ≥ 60% (≈ +4 bps gross per trade)?

## Executive summary

- **No rigorous out-of-sample evidence that a "more powerful" oscillator or regime filter beats
  plain EMA/RSI on crypto at intraday horizons.** Published out-of-sample edges at 10–30 minutes
  are about 0–2 bps gross, i.e. WR ≈ 50–55% on a ±0.20% bracket. A +4 bps edge (WR 60%) from one
  filter would exceed everything published, so an in-sample +4 bps should be presumed overfit.
- **Most indicators carry the same information.** On BTC 5m the usual oscillators correlate
  0.81–0.99 with each other (RSI14 vs our live stretch measure: 0.99), and trend estimators differ
  mainly by lookback, not by formula.
- **What the evidence does support:** state dependence (intraday momentum in high-volume /
  high-volatility sessions, reversal otherwise); breakout entries as an alternative to pullbacks;
  taker-flow imbalance as a conditioner (≈ 1 bp, the only non-price signal that survives minutes
  of latency); trading less via meta-labeling; and rigorous selection (purged walk-forward folds,
  probability of backtest overfitting, SPA, deflated Sharpe).
- **Data:** BingX klines have no taker volume; BingX 1m history starts 2025-08-01. Binance has
  taker volume since 2020; its API is reachable from the VPS but blocked on the user's ISP.

## 1. Predictability at our horizon (5 minutes to a few hours)

- Intraday BTC shows both momentum and reversal, and which one appears depends on jumps,
  liquidity and FOMC days (Wen, Bouri, Xu & Zhao, NAJEF 2022). A rule that always follows the
  trend or always fades should net about zero — consistent with our −0.9 bps for near-mean entries
  in the 1h trend direction.
- Intraday time-series momentum: the first half-hour predicts the last, most strongly in the
  highest-volume and highest-volatility sessions (Shen, Urquhart & Wang, Financial Review 2022).
- Returns at 5m–1h are negatively autocorrelated (−0.10 at 5m, −0.058 at 15m, −0.044 at 30m,
  −0.056 at 1h; Bitstamp 2015–2018) and larger moves revert more (De Nicola, Ledger 2021). That
  implies roughly 3–5 bps of reversal after a 2σ 15-minute move — matching our in-sample fade, in a
  less efficient market than today.
- Abnormal-return days tend to continue to the close, except BTC up-days and ETH down-days, which
  reversed (Caporale & Plastun, Brunel WP 2019, in-sample, no costs).
- Efficiency has risen since 2016 (Şensoy, FRL 2019); the short-horizon Hurst exponent moved from
  ≈ 0.42 (2019–20) to ≈ 0.49 (2021–22) (Tang et al., arXiv 2024, unverified); no profitable BTC/ETH
  rules in 2022–23 after snooping adjustment (Li et al., JAF 2023).
- Quarter-hour structure on Binance perps is real out-of-sample but ≈ 0.5 bp per event, about a
  tenth of one taker fee (Kim & Hansen, arXiv 2026, preprint, unverified).
- Time-of-day effects: only practitioner evidence (Concretum; Quantpedia), contradicted by a
  seven-exchange study finding no persistent pattern (Baur et al. 2017). Insufficient evidence.

## 2. Indicators: new information versus re-parameterisation

Our label-free check (BingX production BTC 1m → 5m, 2026-07-01 to 07-15, 3,721 bars, inside the
training period; no outcomes computed):

| Pair | Spearman ρ |
|---|---|
| RSI14 vs stretch (close − EMA21) / ATR14 | 0.99 |
| Bollinger %B(20) vs CCI(20) | 0.98 |
| RSI5 / RSI14 / Stoch14 / %B / CCI / stretch, pairwise | 0.81–0.95 |
| IBS vs the oscillators above | 0.14–0.42 |
| ConnorsRSI vs RSI14 | 0.45 |
| 4h momentum vs signed efficiency ratio (48 bars) | 0.98 |
| 1h EMA20 − EMA50 vs 5m-based trend measures | 0.05–0.38 |
| ADX14 vs any direction measure | 0.09–0.16 |
| RSI14 vs 4h momentum | 0.67 |

- Every linear trend filter is a weighted sum of past returns; time-series momentum and moving-
  average crossovers are equivalent in general form, so EMA, Hull, SuperSmoother and momentum sign
  share one dimension: the lookback (Levine & Pedersen, FAJ 2016). The weighting shape does not
  reliably improve out-of-sample timing (Zakamulin, SSRN).
- Large rule searches with multiple-testing control find little: about 15,000 rules showed no
  out-of-sample Bitcoin predictability (Hudson & Urquhart, Annals of OR 2021); significant excess
  returns were "rarely achieved" at any sampling frequency (Anghel, FRL 2021); on 5-minute BTC,
  buy-and-hold beat every rule (Resta, Pagnottoni & De Giuli, Risks 2020); the best of 7,846
  intraday rules did not survive (Marshall, Cahan & Cahan, JEF 2008). A counterpoint: combined
  portfolios of 75,360 rules selected net of costs beat buy-and-hold out-of-sample, mainly on a
  risk-adjusted basis (Deprez & Frömmel, IREF 2024).
- ConnorsRSI / RSI(2), IBS, Stochastic RSI, Laguerre RSI, Fisher, KAMA, Supertrend: insufficient
  evidence; RSI(2) and IBS rest on daily equity/ETF backtests.
- Regime measures: a rolling intraday Hurst is mostly estimation noise (reliable DFA needs ~10,000
  points; Yuan et al., Physica A 2018). Efficiency Ratio, Choppiness, regression R², variance
  ratio: insufficient evidence. ADX: one weak, mixed FX study (Gurrib 2018).
- Pullback inside a trend at intraday frequency: insufficient evidence. On daily BTC, buying N-day
  highs held up out-of-sample while buying N-day lows did poorly (Quantpedia / Beluska 2024,
  practitioner, gross of fees).

## 3. Non-price signals

- **Order flow:** signed volume explains about half of same-window 5-minute and hourly returns,
  mostly contemporaneously (Makarov & Schoar, JFE 2020); trade-flow imbalance beats OFI for
  same-window moves (Silantyev, Digital Finance 2019). Truly lagged power sits at sub-second
  horizons (Albers et al., Applied Math. Finance 2022) and decays within seconds. Betting against
  the previous 15-minute candle works in ~90% of Binance pairs, concentrated after taker-driven
  moves, but grosses ≈ 1.3 bp (Kitron & Wengrowicz, arXiv 2026, preprint, unverified). Slow (5–15
  minute) taker-flow conditioning is the only flow signal that survives 1–4 minutes of latency.
- **VWAP / volume profile:** no tested evidence for POC / value area / high-volume nodes; support
  and resistance has weak, decaying evidence (Osler, FRBNY 2000; Chung & Bellotti, arXiv 2021);
  VWAP strategies are practitioner claims only.
- **Funding / open interest / liquidations:** low-frequency evidence only — high carry predicts
  crashes (Schmeling, Schrimpf & Todorov, BIS WP 1087, 2023); no tested evidence at ≤ a few hours;
  some exchanges misquote OI (Giagkiozis & Said, arXiv).

## 4. Methods

- **Model accuracy is modest.** Directional accuracy on BTC was 52.9% at 5m, 53.7% at 15m and
  56.0% at 60m, and every model's strategy was negative after 30 bps round-trip costs (Jaquart,
  Dann & Weinhardt, JFDS 2021). Trading only when the forecast clears costs cut trades from
  10,619 to 251 and turned results positive, yet did not significantly beat buy-and-hold (Bysik &
  Ślepaczuk, arXiv 2026, preprint). Reported 55–65% accuracy without costs or a trading test
  (Akyildirim et al., Annals of OR 2021) is an upper bound.
- **Our bracket is already a triple barrier**, so first-touch labels match the P&L exactly.
  Meta-labeling — the rule picks the side, a model decides whether to take the trade — is the
  recommended structure; validation must purge and embargo overlapping labels and use
  combinatorial purged CV (López de Prado 2018). One E-mini test raised precision from 0.48 to
  0.54 (Hudson & Thames, single source).
- **Searches overfit quickly.** Picking the best k of n signals is biased almost as much as the
  best of nᵏ (Novy-Marx, NBER); 45 independent trials on 5 years can produce an in-sample Sharpe of
  1 from noise (Bailey et al. 2014). Tools: Hansen's SPA and White's Reality Check (Python `arch`),
  the Deflated Sharpe Ratio and the Probability of Backtest Overfitting.
- **How good noise looks for us:** with zero edge, the best of N combinations shows a WR of about
  50% + E_N × 50% / √n (E_N ≈ 2.4 for N = 70, 2.8 for N = 200). At n = 300 trades per combination
  that is 57–58% — almost the target. At n ≈ 2,600 (10 trades/day over the training year) it is
  about 52–53%.
- **Shrinkage to expect:** median Sharpe fell 73% from backtest to live (Suhonen et al., via CXO);
  backtest Sharpe explained under 2.5% of out-of-sample Sharpe across 888 algorithms (Wiecki et al.);
  published anomalies earn 26% less out-of-sample (McLean & Pontiff 2016). A 60% training winner
  should be expected around 53–57% out-of-sample.
- **Power of the holdout:** with ~250–500 holdout trades per symbol, a true 60% has a 95% interval
  of roughly 54–66%. The holdout can show that an edge exists but cannot distinguish 57% from 60%.

## 5. Data availability (checked 2026-10-14)

| Source | What exists | Notes |
|---|---|---|
| BingX production klines | open, high, low, close, volume, time | No taker-buy volume (the GitHub reference claims it; the live endpoint does not return it). 1m from 2025-08-01; 5m ≈ 6 weeks; 1h to ≈ mid-2024; 1d to ≥ 2022 |
| BingX funding history | public, needs startTime + endTime, ≤ 1,000 rows | back to ≥ 2023-01 |
| BingX open interest | current snapshot only | no history |
| BingX public trades | last ≤ 1,000 with `isBuyerMaker` | live CVD possible, no history |
| Binance `/fapi/v1/klines` | adds number of trades [8], taker buy base [9], taker buy quote [10] | blocked on the user's ISP ("Internet Baik"); reachable from the VPS |
| data.binance.vision | 1m klines and aggTrades from 2020; 5m metrics (OI, long/short, taker ratio) from 2020-09; funding; bookDepth from 2023 | metrics gaps and a timestamp convention change on 2026-06-25 (GitHub issues #491, #503, #509) |
| Binance REST stats | OI, taker ratio, long/short | last 30 days only |

## 6. Implications for the design (proposals, pending user approval)

1. Keep the grid on independent axes: trend **lookback** (EMA200 5m, 1h, 4h), **one** oscillator
   (RSI14 — near-identical to the stretch measure the bot already runs), **one** strength family
   (ADX/DI, as the user requested), and the volume-profile location filter the user chose.
2. Add a structural alternative with evidence behind it: **breakout** entries (a new N-bar high or
   low in the trend direction) next to pullback entries.
3. Add one **state** dimension: a rolling volatility/activity percentile, because intraday momentum
   concentrates in high-volume, high-volatility sessions.
4. Select in two stages to keep the effective number of trials small (base rule first, filters
   second), with purged walk-forward folds inside training, ranking on the median fold, at least
   1,000 training trades per eligible combination, and PBO / SPA / deflated metrics reported.
5. Add a **meta-labeling** contender: the winning rule picks the side; a regularised logistic
   regression on 6–8 features decides whether to trade. Validated with purged CV; tested once on
   the holdout.
6. Treat taker-flow conditioning as a later, separate study: it needs the Binance archive and a
   live Binance feed from the VPS (a second market-data exception), and its documented edge is ≈ 1 bp.
7. Make the pass rule power-aware: the holdout can confirm that an edge exists but not that it is
   exactly 60%; the 60% verdict needs more trades (holdout + forward + live).
8. Expectation: the evidence points to WR 50–55% at this bracket; "no pass" is the most likely
   outcome and should be planned for.

## Sources

Intraday predictability and technical rules
1. Wen, Bouri, Xu & Zhao, NAJEF 2022 — https://ideas.repec.org/a/eee/ecofin/v62y2022ics1062940822000833.html
2. Shen, Urquhart & Wang, Financial Review 2022 — https://research.birmingham.ac.uk/en/publications/bitcoin-intraday-time-series-momentum/
3. De Nicola, Ledger 2021 — https://ledger.pitt.edu/ojs/ledger/article/view/213 (also https://epub.ub.uni-muenchen.de/97651/)
4. Caporale & Plastun, Brunel WP 2019 — https://www.brunel.ac.uk/economics-finance-and-accounting/research/pdf/1917-Oct-GMC-Momentum-effect-in-the-Cryptocurrency-Market-after-One-day-Abnormal-Returns.pdf
5. Kim & Hansen, "The Quarter-Hour Effect", arXiv 2026 (preprint) — https://arxiv.org/abs/2607.09426
6. Şensoy, FRL 2019 — https://ideas.repec.org/a/eee/finlet/v28y2019icp68-73.html
7. Tang et al., arXiv 2024 — https://arxiv.org/abs/2402.11930
8. Li et al., JAF 2023 — https://articlegateway.com/index.php/JAF/article/view/6453
9. Concretum, BTC intraday seasonality — https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/
10. Quantpedia, BTC seasonal anomalies — https://quantpedia.com/are-there-seasonal-intraday-or-overnight-anomalies-in-bitcoin/
11. Baur et al. 2017 — https://research-repository.uwa.edu.au/en/publications/bitcoin-time-of-day-day-of-week-and-month-of-year-effects-in-retu/
12. Corbet, Eraslan, Lucey & Sensoy, FRL 2019 — https://doras.dcu.ie/25053
13. Bakker, Erasmus thesis 2018 — https://thesis.eur.nl/pub/41546/
14. Marshall, Cahan & Cahan, JEF 2008 — https://ideas.repec.org/a/eee/empfin/v15y2008i2p199-210.html
15. Hudson & Urquhart, Annals of OR 2021 — https://ideas.repec.org/a/spr/annopr/v297y2021i1d10.1007_s10479-019-03357-1.html
16. Anghel, FRL 2021 — https://ideas.repec.org/a/eee/finlet/v39y2021ics1544612320304414.html
17. Deprez & Frömmel, IREF 2024 — https://ideas.repec.org/a/eee/reveco/v93y2024ipbp858-874.html
18. Resta, Pagnottoni & De Giuli, Risks 2020 — https://www.mdpi.com/2227-9091/8/2/44
19. Gradojevic et al., IJF 2023 — https://ideas.repec.org/a/eee/intfor/v39y2023i1p1-17.html
20. Gerritsen et al., FRL 2020 — https://research-portal.uu.nl/en/publications/the-profitability-of-technical-trading-rules-in-the-bitcoin-marke/
21. Quantpedia / Beluska 2024, trend vs mean reversion in BTC — https://quantpedia.com/revisiting-trend-following-and-mean-reversion-strategies-in-bitcoin/
22. Osler, FRBNY Economic Policy Review 2000 — https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.html

Indicators and regime measures
23. Levine & Pedersen, FAJ 2016 — https://research-api.cbs.dk/ws/files/60084063/lasse_heje_pedersen_et_al_which_trend_is_your_friend_publishersversion.pdf
24. Zakamulin, SSRN — https://papers.ssrn.com/abstract=2677212
25. Baltas & Kosowski, SSRN — https://papers.ssrn.com/abstract=2140091
26. Yuan et al., Physica A 2018 — https://ideas.repec.org/a/eee/phsmap/v505y2018icp179-189.html
27. Gurrib 2018 (ADX, FX) — https://www.businessperspectives.org/index.php/journals/banks-and-bank-systems/issue-291/performance-of-the-average-directional-index-as-a-market-timing-tool-for-the-most-actively-traded-usd-based-currency-pairs
28. Da et al., RFS 2014 (frog-in-the-pan) — https://www3.nd.edu/%7Ezda/Frog.pdf
29. CXO Advisory on RSI(2) — https://www.cxoadvisory.com/technical-trading/a-few-notes-on-short-term-trading-strategies-that-work/
30. LuxAlgo, RSI(2) summary — https://www.luxalgo.com/library/concept/rsi-2/
31. Pandey & Joshi, arXiv 2023 (IBS) — https://arxiv.org/abs/2306.12434
32. Chung & Bellotti, arXiv 2021 — https://arxiv.org/abs/2101.07410

Order flow, VWAP, funding and open interest
33. Makarov & Schoar, JFE 2020 (CFA digest) — https://rpc.cfainstitute.org/research/cfa-digest/2020/10/dig-v50-n10-4
34. Silantyev, Digital Finance 2019 — https://ideas.repec.org/a/spr/digfin/v1y2019i1d10.1007_s42521-019-00007-w.html
35. Albers et al., Applied Mathematical Finance 2022 — https://arxiv.org/abs/2108.09750
36. Kitron & Wengrowicz, arXiv 2026 (preprint) — https://arxiv.org/abs/2608.21888
37. Anastasopoulos & Gradojevic, JFM 2026 — https://www.efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2025-Greece/papers/OrderFlowpaper.pdf
38. Jeon, arXiv 2026 (preprint) — https://arxiv.org/abs/2607.09230
39. Alexander & Heck 2020 — https://sussex.figshare.com/articles/journal_contribution/Price_discovery_in_Bitcoin_the_impact_of_unregulated_markets/23308262
40. Zarattini & Aziz 2023 (VWAP, practitioner) — https://concretumgroup.com/tag/qqq/
41. Backtrex, DAX VWAP band fade (practitioner) — https://backtrex.com/en/backtests/vwap-band-fade-dax
42. Schmeling, Schrimpf & Todorov, BIS WP 1087 — https://www.bis.org/publ/work1087.htm
43. He et al., arXiv — https://arxiv.org/abs/2212.06888
44. Luo, Edinburgh thesis 2025 — https://era.ed.ac.uk/handle/1842/43608
45. Garcia Seuma, arXiv 2026 (preprint) — https://arxiv.org/abs/2607.27070
46. Giagkiozis & Said, arXiv — https://arxiv.org/abs/2310.14973

Methods and overfitting
47. Jaquart, Dann & Weinhardt, JFDS 2021 — https://publikationen.bibliothek.kit.edu/1000150665
48. Bysik & Ślepaczuk, arXiv 2026 (preprint) — https://arxiv.org/abs/2606.00060
49. Akyildirim et al., Annals of OR 2021 — https://repository.bilkent.edu.tr/bitstream/11693/77044/1/Prediction_of_cryptocurrency_returns_using_machine_learning.pdf
50. López de Prado, "The 10 Reasons Most Machine Learning Funds Fail" 2018 — https://www.garp.org/white-paper/the-10-reasons-most-machine-learning-funds-fail
51. Hudson & Thames, meta-labeling test — https://hudsonthames.org/does-meta-labeling-add-to-signal-efficacy/
52. `arch` multiple-comparison docs (SPA, StepM, MCS) — https://arch.readthedocs.io/en/latest/multiple-comparison/multiple-comparison-reference.html
53. Sullivan, Timmermann & White 1999 — https://ideas.repec.org/p/fmg/fmgdps/dp303.html
54. Bailey, Borwein, López de Prado & Zhu 2014 — https://carmamaths.org/jon/backtest.pdf
55. Deflated Sharpe Ratio — https://papers.ssrn.com/abstract=2460551
56. Probability of Backtest Overfitting — https://papers.ssrn.com/abstract=2326253
57. Novy-Marx, NBER — https://www.nber.org/papers/w21329
58. Suhonen et al. 2017 (via CXO) — https://www.cxoadvisory.com/big-ideas/live-performance-of-alternative-beta-products/
59. Wiecki et al. 2016 (via Quantpedia) — https://quantpedia.com/quantopians-academic-paper-about-in-vs-out-of-sample-performance-of-trading-alg/
60. McLean & Pontiff 2016 — https://www.gwern.net/doc/economics/2016-mclean.pdf

Data sources
61. BingX API reference (GitHub) — https://github.com/BingX-API/api-ai-skills
62. Binance public data — https://github.com/binance/binance-public-data (issues #491, #503, #509)
63. Binance connector (Python) — https://github.com/binance/binance-connector-python
64. data.binance.vision listing — https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/daily/
65. Binance HTTP 451 reports — https://community.vercel.com/t/binance-api-calls-on-vercel/29895
66. Binance blocked in China — https://u.today/binance-now-blocked-in-china
67. Binance Hong Kong derivatives exit 2021 — https://www.theblock.co/news/markets/2021-08-06-binance-hong-kong-shuts-crypto-derivatives-trading-after-europe-113743

## Methodology

Three parallel research streams (intraday predictability and technical rules; order flow, VWAP,
funding and data availability; regime filters, model-based methods and overfitting control) using
web search and full-text reads, about 270 tool calls in total. Our own checks: a label-free
correlation analysis on BingX production 1m klines (2026-07-01 to 07-15) and live public-API
probes of BingX and Binance from the Mac and the VPS. Sources were treated as data; none
contained instructions that were followed.
