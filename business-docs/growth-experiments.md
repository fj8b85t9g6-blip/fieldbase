# FieldBase growth experiments

## 2026-07-23 — Instrument the self-serve revenue path

- Verified observation: the product had working registration, job, invoice, and Stripe routes but no durable first-party funnel measurement.
- Customer and entry point: small field-service crew owner arriving through a contractor-specific page or direct visit.
- Hypothesis: if first-touch attribution and activation events survive registration, FieldBase can identify the largest conversion constraint instead of choosing growth work by intuition.
- Primary metric: percentage of trial companies that create their first real job.
- Guardrails: no customer job content in analytics; internal admin companies excluded; billing and job workflows remain functional.
- Minimum evidence: one successful synthetic production journey plus 20 external trial starts or 30 days, whichever comes later.
- Baseline: unknown.
- Implementation: first-party event table, first-touch company attribution, private growth dashboard, and owner activation checklist.
- Decision rule: improve the largest stage-to-stage loss with one bounded experiment; do not scale traffic before production activation and payment attribution pass end to end.
