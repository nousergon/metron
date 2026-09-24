import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Methodology — Metron",
  description: "How Metron computes every figure it shows: returns, attribution, risk, income, and tax lots.",
};

// Public measurement documentation (metron-ops-I205). Every statement on this page
// describes code that ships in this repository — api/services/{performance,attribution,
// risk,diagnostics,tax,technical_rating}.py, portfolio_analytics/domain/{ledger,tax,
// realized,diagnostics}.py, and the nousergon-lib quant primitives they call. When one of
// those changes, this page changes in the same PR. Copy stays descriptive: it says WHAT is
// computed and HOW, never what a reading implies anyone do (guarded by
// __tests__/methodology-page.test.tsx).
const LAST_UPDATED = "September 24, 2026";

const CONTENTS: { id: string; title: string }[] = [
  { id: "valuation", title: "Valuation and data" },
  { id: "performance", title: "Performance: TWR and MWR" },
  { id: "risk-measures", title: "Return-series risk measures" },
  { id: "attribution", title: "Sector attribution" },
  { id: "factor-risk", title: "Factor risk model" },
  { id: "diagnostics", title: "Concentration diagnostics" },
  { id: "income", title: "Realized income" },
  { id: "tax-lots", title: "Tax lots" },
  { id: "technical-rating", title: "Technical rating" },
];

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-6 space-y-3 border-t border-line pt-6">
      <h2 id={`${id}-title`} className="text-base font-semibold">
        <a href={`#${id}`} className="hover:underline">
          {title}
        </a>
      </h2>
      {children}
    </section>
  );
}

function Formula({ children }: { children: ReactNode }) {
  return (
    <p>
      <code className="block overflow-x-auto rounded border border-line bg-panel px-3 py-2 text-xs">{children}</code>
    </p>
  );
}

function Bullets({ children }: { children: ReactNode }) {
  return <ul className="list-disc space-y-1.5 pl-5">{children}</ul>;
}

export default function MethodologyPage() {
  return (
    <article className="max-w-3xl space-y-6 text-sm leading-relaxed text-fg">
      <header className="space-y-2">
        <h1 className="text-xl font-semibold">Methodology</h1>
        <p className="text-muted">Last updated: {LAST_UPDATED}</p>
        <p>
          Metron computes the figures documented here deterministically, from the holdings, transactions, and
          prices of the accounts you connect or import. No language model is involved in computing them. This page
          states how each one is computed. The implementation is open source (AGPL-3.0), so each definition here can be
          checked against the code that produces it.
        </p>
        <p>
          A figure that cannot be computed from the available data is shown as a dash or an explanatory note. Where
          a figure rests on incomplete data, the page says so: for example, reconstructed NAV history is labeled as
          estimated when lot data is incomplete (see{" "}
          <a href="#performance" className="underline">
            Performance
          </a>
          ).
        </p>
        <nav aria-label="Contents" className="rounded border border-line bg-panel p-3">
          <p className="text-xs uppercase tracking-wide text-muted">Contents</p>
          <ol className="mt-2 list-decimal space-y-1 pl-5">
            {CONTENTS.map((s) => (
              <li key={s.id}>
                <a href={`#${s.id}`} className="hover:underline">
                  {s.title}
                </a>
              </li>
            ))}
          </ol>
        </nav>
      </header>

      <Section id="valuation" title="Valuation and data">
        <p>Current holdings are valued as follows:</p>
        <Bullets>
          <li>
            <strong>Price.</strong> A holding is priced at the latest cached daily closing price. When no close is
            cached, the price reported by the broker for that position is used.
          </li>
          <li>
            <strong>Scale cross-check.</strong> When the broker also reports a total market value for the position
            and the computed value (price × quantity) differs from it by more than a factor of two, the
            broker&rsquo;s total is used. This catches a price series and a share count that disagree on scale,
            for example after a split or an ADR ratio change.
          </li>
          <li>
            <strong>Currency.</strong> Values are converted to the portfolio&rsquo;s base currency at a cached
            exchange rate. A holding whose currency has no cached rate is left out of base-currency totals rather
            than converted at 1:1. Historical figures (realized gains, dividends, reconstructed NAV) use the rate
            as of the event or valuation date; reconstructed NAV falls back to the most recent cached rate when no
            dated rate is available.
          </li>
          <li>
            <strong>Settled data.</strong> Performance, Attribution, Risk, Diagnostics, and Tax are computed from
            closing prices; the first four show the close date their data runs through. Intraday prices appear only
            on the live Overview and Holdings views.
          </li>
          <li>
            <strong>Scope.</strong> Selecting accounts in the account panel restricts each computation to those
            accounts.
          </li>
        </Bullets>
      </Section>

      <Section id="performance" title="Performance: TWR and MWR">
        <p>
          <strong>NAV series.</strong> Each day, Metron records one NAV snapshot: the sum of the base-currency
          market values of the portfolio&rsquo;s priced holdings. Alongside it, the snapshot records the day&rsquo;s{" "}
          <em>external flow</em> F: capital that entered or left the valued holdings since the previous snapshot,
          measured as the amount of purchases minus the proceeds of sales. With flows recorded, a contribution
          reads as capital in, not as investment return.
        </p>
        <p>
          <strong>Time-weighted return (TWR).</strong> Snapshots are recorded after the day&rsquo;s flows, so
          each sub-period return removes the flow before comparing to the prior NAV. The sub-period returns are
          linked geometrically:
        </p>
        <Formula>
          r<sub>t</sub> = (NAV<sub>t</sub> − F<sub>t</sub>) / NAV<sub>t−1</sub> − 1 &nbsp;&nbsp;·&nbsp;&nbsp; TWR = ∏
          (1 + r<sub>t</sub>) − 1
        </Formula>
        <p>
          If a sub-period starts from a non-positive NAV, the headline TWR is not shown, and the cumulative figure
          and risk measures skip that sub-period. The &ldquo;Cumulative&rdquo; figure on the Performance page is
          this same geometric link over the full history.
        </p>
        <p>
          <strong>Money-weighted return (MWR).</strong> MWR is the internal rate of return (XIRR) of the
          investor&rsquo;s cash flows over the window, built from the same flow series as TWR: the starting NAV as
          an outflow on the first date, each later snapshot&rsquo;s net flow as an outflow (or inflow, for net
          sales), and the ending NAV as a final inflow. The rate is solved on an actual/365 day count (Newton&rsquo;s
          method with a bisection fallback). When the flows never change sign, or the solver does not converge,
          MWR is not shown. The window figure is the annual rate compounded over the window&rsquo;s length:
        </p>
        <Formula>
          MWR<sub>window</sub> = (1 + XIRR)<sup>days / 365</sup> − 1
        </Formula>
        <p>
          TWR removes the effect of when and how much capital was added. MWR includes it. The two differ whenever
          flows occur during the window.
        </p>
        <p>
          <strong>Annualization.</strong> Annualized TWR and MWR use (1 + R)<sup>365 / days</sup> − 1 and are shown
          only once the window spans at least 30 days. Shorter windows show the window return only, because
          annualizing a few days of return produces extreme figures.
        </p>
        <p>
          <strong>Overview period tiles.</strong> Today, YTD, and LTM each take a window of snapshots and report
          the dollar gain (ending NAV − starting NAV − the flows inside the window), TWR, and MWR over it.
        </p>
        <Bullets>
          <li>
            <em>Today</em> is the change from the previous NYSE session&rsquo;s snapshot to the last closed
            session&rsquo;s snapshot. Snapshots stamped on weekends or holidays are skipped. When the last closed
            session is not the current calendar day, the tile is labeled &ldquo;as of&rdquo; that date.
          </li>
          <li>
            <em>YTD</em> starts at the last snapshot of the prior calendar year; <em>LTM</em> at the last snapshot
            on or before the date 365 days ago.
          </li>
          <li>
            Where benchmark prices are available, each tile also shows the price return of SPY, QQQ, and IWM (the
            ETFs used as proxies for the S&amp;P 500, Nasdaq-100, and Russell 2000) between the same two dates,
            and the difference between the portfolio&rsquo;s TWR and each one.
          </li>
        </Bullets>
        <p>
          <strong>Difference vs SPY.</strong> The Performance page&rsquo;s &ldquo;Alpha vs SPY&rdquo; is the
          portfolio&rsquo;s TWR minus SPY&rsquo;s price return between the first and last snapshot. It is a
          simple difference of two returns, not a regression alpha.
        </p>
        <p>
          <strong>Building history from past prices.</strong> NAV snapshots accumulate forward, one per day.
          Earlier history can be reconstructed from tax lots and the transaction ledger: the portfolio is valued
          at every month-end and on every date a flow occurs, using the closing price on or before that date. For
          accounts rebuilt from broker lots, a lot opening counts as capital in and a lot closing as capital out,
          each valued at that day&rsquo;s price exactly as NAV values it, so a change in composition does not
          register as a return. A holding with no lot or transaction history cannot be placed on past dates, so it
          is carried at its current value across the whole history rather than dropped. When the open lots of any
          current holding do not reconcile to its position quantity (within 1%), the Performance page labels the
          history as estimated and names the affected holdings.
        </p>
        <p>
          <strong>Data guards.</strong> A new snapshot is not recorded when its NAV is more than 3× or less than
          one-third of the median of up to seven prior snapshots plus the day&rsquo;s flow, a move no recorded flow
          explains. A snapshot taken while a mutual fund&rsquo;s closing NAV has not yet been published is marked
          provisional and restated, within seven days, once that NAV is available.
        </p>
        <p>
          Metrics are shown only once there are at least two snapshots. Scoped to a subset of accounts, the series
          uses those accounts&rsquo; own daily snapshots and starts on the first date every selected account has
          one.
        </p>
      </Section>

      <Section id="risk-measures" title="Return-series risk measures">
        <p>
          The risk statistics on the Performance page are computed on the flow-adjusted sub-period returns
          r<sub>t</sub> defined above, so a contribution never registers as volatility.
        </p>
        <p>
          <strong>Periods per year.</strong> Snapshots do not necessarily fall on every trading day, so
          annualization uses the observed sampling rate instead of assuming 252: periods per year = (number of
          returns) / (days elapsed / 365.25).
        </p>
        <Bullets>
          <li>
            <strong>Volatility</strong>: the sample standard deviation of r<sub>t</sub> × √(periods per year).
          </li>
          <li>
            <strong>Sharpe ratio</strong>: mean(r<sub>t</sub>) / standard deviation(r<sub>t</sub>) × √(periods per
            year), with a 0% risk-free rate.
          </li>
          <li>
            <strong>Sortino ratio</strong>: mean(r<sub>t</sub>) / downside deviation × √(periods per year). Downside
            deviation is the root mean square of min(0, r<sub>t</sub>) over all observations, with a 0% target.
          </li>
          <li>
            <strong>PSR</strong> (Probabilistic Sharpe Ratio, Bailey &amp; López de Prado 2012): the estimated
            probability that the true Sharpe ratio of the observed return series is above zero, adjusting for the
            sample&rsquo;s length, skewness, and kurtosis. Requires at least 30 returns.
          </li>
          <li>
            <strong>Max drawdown</strong>: the largest peak-to-trough decline of the growth index ∏(1 + r
            <sub>t</sub>), expressed as a negative percentage.
          </li>
          <li>
            <strong>CVaR (95%)</strong>: the historical conditional value at risk: the mean of the losses at or
            beyond the 95th-percentile historical loss, shown as a negative return.
          </li>
        </Bullets>
        <p>
          Volatility, Sharpe, Sortino, and PSR are shown only once the history spans at least 30 days. Max
          drawdown and CVaR do not depend on annualization and are shown at any length.
        </p>
        <p>
          <strong>Risk over time.</strong> The rolling chart recomputes the same measures over a trailing window
          of up to 63 returns (about three months of trading days). A point appears once the window holds at least
          20 returns spanning at least 30 days.
        </p>
      </Section>

      <Section id="attribution" title="Sector attribution">
        <p>
          The Attribution page splits the difference between the portfolio&rsquo;s return and SPY&rsquo;s return
          over the trailing 90 days into three sector-level effects, using the
          Brinson-Fachler method. For each sector i:
        </p>
        <Formula>
          Allocation<sub>i</sub> = (w<sub>p,i</sub> − w<sub>b,i</sub>) × (r<sub>b,i</sub> − R<sub>b</sub>)
          <br />
          Selection<sub>i</sub> = w<sub>b,i</sub> × (r<sub>p,i</sub> − r<sub>b,i</sub>)
          <br />
          Interaction<sub>i</sub> = (w<sub>p,i</sub> − w<sub>b,i</sub>) × (r<sub>p,i</sub> − r<sub>b,i</sub>)
        </Formula>
        <p>
          where R<sub>p</sub> = Σ w<sub>p,i</sub> r<sub>p,i</sub> and R<sub>b</sub> = Σ w<sub>b,i</sub> r
          <sub>b,i</sub>. Summed over sectors, the three effects equal R<sub>p</sub> − R<sub>b</sub>.
        </p>
        <Bullets>
          <li>
            <strong>Portfolio weights</strong> w<sub>p,i</sub>: each GICS sector&rsquo;s share of the market value
            of holdings that have a resolved sector. The share of market value that is covered this way is shown
            as coverage; holdings without a sector are reported, not assigned to one.
          </li>
          <li>
            <strong>Portfolio sector returns</strong> r<sub>p,i</sub>: the market-value-weighted average of each
            holding&rsquo;s price return over the window, from the first close on or after the window start to
            the latest close.
          </li>
          <li>
            <strong>Benchmark weights</strong> w<sub>b,i</sub>: the benchmark ETF&rsquo;s published sector
            weights, restricted to the eleven GICS sectors and rescaled to sum to 1.
          </li>
          <li>
            <strong>Benchmark sector returns</strong> r<sub>b,i</sub>: the price return over the same window of
            each sector&rsquo;s Select Sector SPDR ETF (XLK, XLF, XLV, XLY, XLP, XLE, XLI, XLB, XLU, XLRE, XLC).
          </li>
        </Bullets>
        <p>
          <strong>What this approximation does and does not capture.</strong> Weights are the holdings&rsquo;
          current market-value weights, held constant over the window, so trades made during the window are not
          reflected. The calculation is single-period over the whole window, not a chain of linked daily periods.
          R<sub>b</sub> is built from sector ETF returns at the benchmark&rsquo;s sector weights, so it can differ
          slightly from the benchmark ETF&rsquo;s own return. A sector the portfolio does not hold has zero
          selection effect; a sector the benchmark does not hold takes R<sub>b</sub> as its benchmark return.
        </p>
      </Section>

      <Section id="factor-risk" title="Factor risk model">
        <p>
          <strong>What the model is.</strong> The Risk page uses a time-series regression of each holding&rsquo;s
          daily returns on six factor return series built from exchange-traded funds. It is not a Barra-style
          fundamental risk model and not a Fama-French model. The factors are:
        </p>
        <Bullets>
          <li>
            <strong>Market</strong>: SPY&rsquo;s daily return.
          </li>
          <li>
            <strong>Five style factors</strong>: the daily return of an iShares MSCI USA factor ETF minus SPY&rsquo;s
            return on the same day: Momentum (MTUM), Quality (QUAL), Low volatility (USMV), Value (VLUE), and Size
            (SIZE). Subtracting SPY separates each style tilt from market exposure. The factors inherit these funds&rsquo;
            index construction. A style factor whose ETF history is missing is left out of the model.
          </li>
        </Bullets>
        <p>
          <strong>Estimation.</strong> Daily returns come from the cached closing prices of the holdings and the
          factor ETFs; computing risk first fetches the last 403 calendar days of them (roughly one year of trading
          sessions), and the Risk page shows how many daily observations were used. The model uses the dates on which every factor series has a return,
          trimmed at the end to the last date that at least 80% of holdings also have. A holding is modeled only if
          it has a return on every one of those dates and at least 11 observations; the rest are listed as
          excluded. Then:
        </p>
        <Bullets>
          <li>
            Each holding&rsquo;s returns are regressed on the factors by ordinary least squares with an intercept,
            giving factor loadings (the matrix B) and a residual variance (the diagonal matrix D, with n − k − 1
            degrees of freedom).
          </li>
          <li>
            The factor covariance matrix F is the Ledoit-Wolf shrinkage estimate: the sample covariance of the
            factor returns shrunk toward a scaled identity matrix, with shrinkage intensity estimated from the
            data.
          </li>
          <li>
            The holdings&rsquo; covariance is modeled as Σ = B F B<sup>ᵀ</sup> + D.
          </li>
        </Bullets>
        <p>
          <strong>Outputs.</strong> Weights w are the modeled holdings&rsquo; current market-value weights,
          rescaled to sum to 1. With x = B<sup>ᵀ</sup>w (the portfolio&rsquo;s net factor exposure), daily
          variances are annualized by 252 trading days:
        </p>
        <Formula>
          factor vol = √(252 × xᵀ F x) &nbsp;&nbsp;·&nbsp;&nbsp; idiosyncratic vol = √(252 × Σ w<sub>i</sub>
          <sup>2</sup> D<sub>i</sub>) &nbsp;&nbsp;·&nbsp;&nbsp; total vol = √(252 × (xᵀ F x + Σ w<sub>i</sub>
          <sup>2</sup> D<sub>i</sub>))
        </Formula>
        <Bullets>
          <li>
            <strong>Factor exposures</strong> are the entries of x.
          </li>
          <li>
            <strong>Variance contribution</strong> of factor k is x<sub>k</sub> (F x)<sub>k</sub> divided by total
            variance; the idiosyncratic share is the remainder.
          </li>
          <li>
            <strong>Tracking error vs SPY</strong>: SPY&rsquo;s own returns are regressed on the same factors to get
            its exposure x<sub>b</sub>. Active exposure a = x − x<sub>b</sub>, and tracking error = √(252 × (aᵀ F a
            + Σ w<sub>i</sub>
            <sup>2</sup> D<sub>i</sub>)). The portfolio&rsquo;s idiosyncratic variance counts as fully active.
          </li>
        </Bullets>
        <p>
          &ldquo;Ex-ante&rdquo; here means the current weights applied to covariances estimated from past daily
          returns. The figures describe how the current holdings varied together over the sample. They are not a
          projection of future volatility.
        </p>
      </Section>

      <Section id="diagnostics" title="Concentration diagnostics">
        <p>
          The Diagnostics page measures portfolio structure from settled market values. Cash, unpriced
          holdings, and holdings without a base-currency value are left out, and watchlist entries are never
          included.
        </p>
        <Bullets>
          <li>
            <strong>HHI</strong> (Herfindahl-Hirschman index): Σ w<sub>i</sub>
            <sup>2</sup> over position weights. It equals 1/N for N equal positions and 1 for a single position.
          </li>
          <li>
            <strong>Effective number of positions</strong>: 1 / HHI, the number of equal-weight positions with the
            same concentration.
          </li>
          <li>
            <strong>Top-5 and top-10 share</strong>: the combined weight of the five and ten largest positions; and
            the largest single position with its weight.
          </li>
          <li>
            <strong>Sector weights vs SPY</strong>: each sector&rsquo;s share of included market value next to
            SPY&rsquo;s sector weight, and the difference. Unheld benchmark sectors appear at 0%; holdings without a
            sector appear as Unclassified.
          </li>
          <li>
            <strong>Geography</strong>: US, International, or Unclassified, by the country of domicile of each
            holding.
          </li>
          <li>
            <strong>Your stated targets</strong>: where you have entered targets, each is compared mechanically
            with the measured value (&ldquo;you set X; actual is Y&rdquo;): a US/International allocation, a maximum
            single-position weight, and sectors you listed to avoid. A target Metron cannot measure is reported as
            not measurable.
          </li>
        </Bullets>
      </Section>

      <Section id="income" title="Realized income">
        <p>
          The Tax page&rsquo;s income table sums, per calendar year, the following. Gains, dividends, and interest
          cover taxable accounts only.
        </p>
        <Bullets>
          <li>
            <strong>Realized short-term and long-term gains</strong> from closed lots (see{" "}
            <a href="#tax-lots" className="underline">
              Tax lots
            </a>
            ), assigned to the year of the closing date. For accounts where the broker reports closed lots itself,
            those lots are used instead of replaying the transactions, so no disposal is counted twice.
          </li>
          <li>
            <strong>Dividends and interest</strong>: the dividend and interest cash transactions, assigned to the
            year of their trade date.
          </li>
          <li>
            <strong>Distributions</strong>: withdrawals from tax-deferred accounts (for example a traditional IRA or
            401(k)), shown as a separate column when present.
          </li>
        </Bullets>
        <p>
          Each amount is converted to the base currency at the exchange rate on its own event date; an amount with
          no available rate is left out. The current year is year-to-date. These are totals of what the
          account data shows. They are not a tax form, and the dividend line does not separate qualified from
          non-qualified dividends.
        </p>
      </Section>

      <Section id="tax-lots" title="Tax lots">
        <p>
          <strong>Lot ledger.</strong> Transactions are replayed in date order, separately for each account and
          security, into tax lots:
        </p>
        <Bullets>
          <li>
            A purchase opens a lot at its price; fees are added to the cost basis. When the price is missing
            (common for money-market sweeps), cost per share is the transaction amount divided by quantity.
          </li>
          <li>
            A sale closes lots first-in, first-out (FIFO), within the same account only. Fees reduce the proceeds.
            Each closed lot, or part of one, becomes a realized-gain record with its proceeds, cost basis, and
            holding period.
          </li>
          <li>A split multiplies each open lot&rsquo;s shares by the ratio and divides its cost per share by it.</li>
          <li>
            A sale larger than the shares the history shows, which happens when a broker&rsquo;s activity feed starts
            partway through a position, means that security&rsquo;s lots cannot be reconstructed. That
            security&rsquo;s history in that account is left out of the lot view and named on the page.
          </li>
        </Bullets>
        <p>
          <strong>Holding period.</strong> A lot held more than 365 days is long-term; otherwise short-term. A lot
          with no determinable holding period is grouped with short-term.
        </p>
        <p>
          <strong>Unrealized gain</strong> for an open lot = quantity × latest close × exchange rate − cost basis in
          the base currency. A lot without a cached price or rate shows its cost basis and term but no market value.{" "}
          <strong>Harvestable loss</strong> for a lot is the size of its unrealized loss when it is below cost, and
          zero otherwise. The page total is the sum over lots.
        </p>
        <p>
          <strong>Reconciliation.</strong> &ldquo;Total unrealized&rdquo; is computed from current positions (the
          same values as the Accounts table), so it includes positions whose lots could not be reconstructed. The
          lot-classified short- and long-term figures cover only reconstructable lots; the page shows the
          difference and names the positions it comes from.
        </p>
        <p>
          <strong>Realized lots</strong> are converted to the base currency at the exchange rate on the closing
          date. Tax-advantaged accounts (IRA, 401(k), Roth) are excluded from the Tax page, and the number excluded
          is shown.
        </p>
      </Section>

      <Section id="technical-rating" title="Technical rating">
        <p>
          The technical rating shown on Holdings and security pages is a signed score from −1 to +1 with a
          five-step label. It is a composite vote across moving-average and oscillator indicators; the
          moving-average and oscillator sub-scores appear on each security&rsquo;s page. The score is computed from
          price history by Metron&rsquo;s market-data pipeline and read as published; the analytics service does
          not adjust it.
        </p>
        <Bullets>
          <li>
            <strong>Timing.</strong> An intraday rating, computed from delayed intraday prices, is used for a
            security only while the published intraday ratings are no more than 20 minutes old. Otherwise the
            end-of-day rating is used. The basis (intraday or end-of-day) is shown with the rating.
          </li>
          <li>
            <strong>What it is.</strong> The rating is descriptive: it summarizes where each indicator stands on the
            current price history. It makes no claim about future returns.
          </li>
          <li>
            <strong>Track record.</strong> Where available, the Diagnostics page shows the rating&rsquo;s measured
            track record: realized forward returns for each label, at several horizons, with the information
            coefficient shown next to its noise floor. The backfill segment is simulated on today&rsquo;s set of
            securities, so it carries survivorship bias. Only the live segment is out-of-sample.
          </li>
        </Bullets>
      </Section>

      <p className="rounded border border-line bg-panel p-3 text-xs text-muted">
        This page documents the calculations as implemented. Figures depend on the completeness and accuracy of
        the account data and prices they are computed from; see the{" "}
        <Link href="/terms" className="underline hover:text-fg">
          Terms
        </Link>{" "}
        for the Service&rsquo;s descriptive-only scope.
      </p>
    </article>
  );
}
