# Pulsentra product refresh plan

Updated: 2026-10-30

## Product direction

Move Pulsentra from a research-oriented single-page dashboard toward a dependable market-intelligence product. Preserve the existing research and backtest engine; improve the presentation layer without implying that unvalidated strategies are profitable.

## Data-source policy

A source is eligible for production only after its access method, terms, attribution, freshness, and redistribution rights are documented. “Publicly viewable” does not mean “free to scrape, store, or redistribute”.

| Data | Candidate | Credentials / account | Initial use | Guardrail |
|---|---|---|---|---|
| Historical Korean OHLCV | Existing local research archive | No new account; data already collected | Local research only | Naver fchart is not an official API; robots.txt and commercial rights are unresolved. Do not resume bulk collection or publish this dataset. |
| Listed instrument master | FinanceDataReader KRX / KRX delisting snapshot | No broker account; third-party GitHub cache | Local reference, pending terms review | Store snapshot date and provider provenance; do not treat as authoritative real-time universe. |
| Official listed-stock master | Financial Services Commission / data.go.kr KRX Listed Stock Information | Public-data portal service key and application | Instrument master enrichment | Application required; check current quota, field definitions, and redistribution terms. Not a broker account. |
| Daily market statistics / indices | KRX Open API | KRX developer key/application | Research cross-check only | Existing repository documents restrictive non-commercial and no-third-party-provision terms. Keep blocked from app DB/public dashboard unless a suitable licence is confirmed. |
| Disclosures and financial statements | OpenDART | OpenDART key; no securities account | Filing timeline, financial statements, corporate actions | Respect API limits; record receipt date and original filing/correction relationships. |
| Macro indicators | Bank of Korea ECOS | ECOS key/application | Policy rate, FX, CPI, exports and other context series | Validate units, revision dates, frequency, and publication lag. |
| Live quote / broker feed | KIS Open API | KIS account and app credentials | Later phase | Read-only endpoints first; no order routes. WebSocket is separate work. |
| News | Official issuer / regulator RSS and licensed feeds | Depends on publisher | Source-linked headlines only | No unlicensed article scraping or full-text republication. Record publisher, URL, timestamp, and short factual summary. |

### Data acquisition order

1. Keep the current local archive read-only and available for reproducible research.
2. Add an instrument-master refresh using a documented, approved source (data.go.kr application is independent of a brokerage account).
3. Add OpenDART disclosure / financial-statement sync after the user obtains a DART key. This does not require a securities account.
4. Add ECOS macro series with clear units and publication dates.
5. Revisit market-price ingestion only after terms permit it. Until then, use user-provided CSV or licensed feed for production price displays.
6. Later, add KIS quote and daily-bar provider when the user has a securities account and KIS app credentials.

Every record displayed in the product should carry: provider, source type, fetched_at, observation date, freshness status, and any adjustment / coverage caveat. Never blend DEMO, TEST, restricted research data, and production data without explicit source-type boundaries.

## Dashboard design direction

- Clean, restrained fintech visual language: warm light canvas, white surfaces, subtle borders, restrained teal/blue accent, readable typography, consistent spacing.
- Preserve all existing element IDs, API contracts, tab navigation, and canvas rendering; this PR is a presentation-only refresh.
- Keep market-data status and source/freshness visible. Avoid fake live indicators.
- Emphasize the market overview, watchlist/scanner, index context, latest disclosures, and data-quality status before advanced research controls.
- Responsive layouts should keep key cards and tables usable on desktop and mobile.
- Do not display invented prices, rankings, returns, or model confidence.

## Implementation sequence

- [x] Apply a presentation-only visual refresh to the existing dashboard stylesheet.
- [ ] Review the refreshed UI against desktop and mobile screenshots.
- [ ] Add a dedicated market overview / index-context endpoint backed only by eligible data.
- [ ] Add instrument master import and provenance tests.
- [ ] Add OpenDART and ECOS data cards after credentials and provider terms are verified.
- [ ] Reorganize navigation and dashboard information hierarchy without breaking existing tabs.
- [ ] Add integration tests for source provenance, freshness, and no cross-contamination of DEMO/REAL/TEST.
- [ ] Run the full offline test suite and deploy only after review.

## Non-goals for this phase

- No securities orders, portfolio connection, or broker account access.
- No unlicensed real-time quote feed.
- No unverified scraping of finance portals or news sites.
- No claims of predictive alpha or guaranteed investment returns.
