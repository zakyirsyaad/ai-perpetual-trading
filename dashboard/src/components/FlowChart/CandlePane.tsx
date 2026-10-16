"use client";

import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Candle, FillMark } from "@/lib/ohlc";

export type EntryLine = {
  price: number;
  side: "long" | "short";
};

type Props = {
  candles: Candle[];
  marks: FillMark[];
  entry: EntryLine | null;
  rangeKey: string;
  visibleBars: number;
  secondsVisible: boolean;
  formatPrice: (n: number) => string;
};

const UP = "#000000";
const DOWN = "#ffffff";
const BUY = "#00aa00";
const SELL = "#cc0000";

function asTime(sec: number): UTCTimestamp {
  return sec as UTCTimestamp;
}

function toBars(rows: Candle[]): CandlestickData<Time>[] {
  return rows.map((c) => ({
    time: asTime(c.time),
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
  }));
}

function toMarkers(rows: FillMark[]): SeriesMarker<Time>[] {
  return rows.map((m) => ({
    time: asTime(m.time),
    position: m.side === "buy" ? "belowBar" : "aboveBar",
    shape: m.side === "buy" ? "arrowUp" : "arrowDown",
    color: m.side === "buy" ? BUY : SELL,
    size: 0.8,
  }));
}

function stemOf(rows: Candle[]): string {
  if (!rows.length) return "empty";
  return `${rows[0]!.time}`;
}

/** True when the visible range reaches the last of `count` drawn bars (or nothing is shown yet). */
function atLiveEdge(chart: IChartApi | null, count: number): boolean {
  const range = chart?.timeScale().getVisibleLogicalRange();
  return !range || range.to >= count - 1;
}

function showLatest(chart: IChartApi | null, count: number, visibleBars: number) {
  if (!chart || count <= 0) return;
  const to = count + 1;
  const from = Math.max(-1, to - visibleBars);
  chart.timeScale().setVisibleLogicalRange({ from, to });
}

export default function CandlePane({
  candles,
  marks,
  entry,
  rangeKey,
  visibleBars,
  secondsVisible,
  formatPrice,
}: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const entryLineRef = useRef<IPriceLine | null>(null);
  const stemRef = useRef("");
  // Open time of the newest bar on the series: update() may only touch it or a later bar.
  const lastTimeRef = useRef(0);
  const barCountRef = useRef(0);
  const rangeRef = useRef("");
  const formatRef = useRef(formatPrice);
  formatRef.current = formatPrice;

  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    const chart = createChart(el, {
      width: Math.max(1, el.clientWidth),
      height: Math.max(1, el.clientHeight),
      layout: {
        background: { type: ColorType.Solid, color: "#ffffff" },
        textColor: "#666666",
        fontFamily: "IBM Plex Mono, ui-monospace, monospace",
      },
      grid: {
        vertLines: { color: "#e5e5e5" },
        horzLines: { color: "#e5e5e5" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#000000", scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: {
        borderColor: "#000000",
        timeVisible: true,
        secondsVisible,
        shiftVisibleRangeOnNewBar: true,
      },
      localization: { priceFormatter: (p: number) => formatRef.current(p) },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: UP,
      wickUpColor: UP,
      wickDownColor: UP,
    });
    const markers = createSeriesMarkers(series, []);
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (!r) return;
      chart.resize(Math.max(1, Math.round(r.width)), Math.max(1, Math.round(r.height)));
    });
    ro.observe(el);
    chartRef.current = chart;
    seriesRef.current = series;
    markersRef.current = markers;
    return () => {
      ro.disconnect();
      markers.detach();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      markersRef.current = null;
      entryLineRef.current = null;
      stemRef.current = "";
      lastTimeRef.current = 0;
      barCountRef.current = 0;
      rangeRef.current = "";
    };
  }, []);

  useEffect(() => {
    chartRef.current?.applyOptions({ timeScale: { secondsVisible, timeVisible: true } });
  }, [secondsVisible]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    const stem = stemOf(candles);
    if (!candles.length) {
      series.setData([]);
      stemRef.current = stem;
      lastTimeRef.current = 0;
      barCountRef.current = 0;
      return;
    }
    const stemChanged = stemRef.current !== stem;
    const rangeChanged = rangeRef.current !== rangeKey;
    const newest = candles[candles.length - 1]!;
    const added = candles.length - barCountRef.current;
    // update() only touches the newest bar, so it is safe only for the same coin
    // and interval, the same first bar, and zero or one new bar whose time does
    // not go back. Anything else redraws: an older newest bar would make
    // lightweight-charts throw "Cannot update oldest data" and take the page
    // down, a shrunk series would keep stale bars, and a jump of several bars
    // would skip the ones in between.
    const canUpdate =
      !stemChanged && !rangeChanged && newest.time >= lastTimeRef.current && (added === 0 || added === 1);
    if (canUpdate) {
      series.update(toBars([newest])[0]!);
    } else {
      // A new coin or interval always opens on the latest bars. Any other redraw
      // (a catch-up of several bars, a tape whose oldest bar was trimmed or that
      // was rebuilt) keeps following the live edge only if the view was on it,
      // so a view scrolled back to an earlier entry stays where it is.
      const followLive = rangeChanged || atLiveEdge(chartRef.current, barCountRef.current);
      const bars = toBars(candles);
      series.setData(bars);
      stemRef.current = stem;
      rangeRef.current = rangeKey;
      if (followLive) showLatest(chartRef.current, bars.length, visibleBars);
    }
    lastTimeRef.current = newest.time;
    barCountRef.current = candles.length;
  }, [candles, rangeKey, visibleBars]);

  useEffect(() => {
    markersRef.current?.setMarkers(toMarkers(marks));
  }, [marks]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    if (!entry || !(entry.price > 0)) {
      if (entryLineRef.current) {
        series.removePriceLine(entryLineRef.current);
        entryLineRef.current = null;
      }
      return;
    }
    const next = {
      price: entry.price,
      color: entry.side === "long" ? BUY : SELL,
      lineWidth: 1 as const,
      lineStyle: LineStyle.Solid,
      axisLabelVisible: true,
      title: "entry",
    };
    if (entryLineRef.current) {
      entryLineRef.current.applyOptions(next);
      return;
    }
    entryLineRef.current = series.createPriceLine(next);
  }, [entry]);

  return <div ref={hostRef} className="lwc-host" style={{ position: "absolute", inset: 0 }} />;
}
