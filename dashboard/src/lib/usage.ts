import type { UsageDay, UsageSummary } from "@/lib/bot-types";

/** Input, reasoning and the visible answer; together they add up to the day's total. */
export interface TokenMix {
  input: number;
  reasoning: number;
  answer: number;
}

/** `outputTokens` already contains the reasoning, so the answer is what is left of it. */
export function tokenMix(day: Pick<UsageDay, "inputTokens" | "outputTokens" | "reasoningTokens">): TokenMix {
  const reasoning = Math.min(Math.max(day.reasoningTokens, 0), Math.max(day.outputTokens, 0));
  return {
    input: Math.max(day.inputTokens, 0),
    reasoning,
    answer: Math.max(day.outputTokens - reasoning, 0),
  };
}

/** Tokens spent per closed trade, or null while nothing has closed (a division would lie). */
export function tokensPerTrade(totalTokens: number, closedTrades: number): number | null {
  return closedTrades > 0 ? totalTokens / closedTrades : null;
}

export interface UsageTotals {
  calls: number;
  failed: number;
  totalTokens: number;
  closedTrades: number;
}

/** Everything since the cycle start, for the "Σ" line. */
export function sumDays(days: readonly UsageDay[]): UsageTotals {
  return days.reduce<UsageTotals>(
    (sum, d) => ({
      calls: sum.calls + d.calls,
      failed: sum.failed + d.failed,
      totalTokens: sum.totalTokens + d.totalTokens,
      closedTrades: sum.closedTrades + d.closedTrades,
    }),
    { calls: 0, failed: 0, totalTokens: 0, closedTrades: 0 },
  );
}

/** The bot always lists today, even before its first call; null only before the first update. */
export function todayOf(usage: UsageSummary | null): UsageDay | null {
  return usage?.days.find((d) => d.day === usage.today) ?? null;
}

/** Width in percent of `part` out of `whole`, clamped to the bar. */
export function share(part: number, whole: number): number {
  return whole > 0 ? Math.min(100, Math.max(0, (part / whole) * 100)) : 0;
}
