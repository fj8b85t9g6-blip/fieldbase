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

## 2026-07-28 — Convert registration starts into trials

- Product: FieldBase.
- Verified observation: production recorded 137 distinct landing visitors and 10 registration starts, but no completed trial registrations or active subscriptions. The existing events could not distinguish form abandonment from failed submissions.
- Customer and entry point: owner of a 1–5 person field-service crew arriving directly or through a contractor-specific acquisition page.
- Hypothesis: if the signup page makes the no-card trial, setup time, and immediate outcome explicit; preserves non-sensitive fields after a validation error; and prevents double submission, more registration starters will complete a trial because perceived commitment and retry friction are lower.
- Primary metric: completed trial registrations divided by distinct registration starters.
- Guardrails: no password retention; no new personal data in analytics; registration, login, job, and billing tests remain green; internal synthetic activity remains excluded from external-customer conclusions.
- Minimum evidence: 20 new external registration starts or 30 days after production deployment, whichever comes later.
- Baseline: 0 completed trials from 10 recorded registration starts. Traffic qualification remains uncertain because all starts were attributed as direct.
- Implementation: explicit trial assurance, outcome-focused CTA, preserved non-sensitive form values, server-side password-length validation, submission-state handling, and a new `registration_form_submitted` funnel stage.
- Decision rule: if form submissions remain near zero, change acquisition targeting or add a product preview before signup. If submissions occur but trials do not, diagnose validation or server failures. If trials complete, move to first-job activation before increasing traffic.

## 2026-07-31 — Put first value before registration

- Product: FieldBase.
- Verified observation: production recorded 207 distinct landing visitors, 19 registration starts, zero submitted registration forms, and zero completed trials. The July 28 signup-friction experiment therefore reached its explicit branch to add a product preview before signup.
- Customer and entry point: owner of a 1–5 person field-service crew arriving on the homepage or a contractor-specific acquisition page.
- Hypothesis: if a visitor can move a realistic sample job from dispatch to invoice without an account, more qualified visitors will experience the core value and choose to begin registration.
- Primary metric: distinct external demo completions divided by distinct external demo starts.
- Secondary metric: distinct trial clicks after demo completion; registration submissions and completed trials remain downstream outcomes.
- Guardrails: sample data stays in the signed session; the demo creates no company, job, client, user, or invoice records; internal and synthetic sources remain excluded; the direct free-trial path remains available.
- Minimum evidence: 20 external demo starts or 14 days after production deployment, whichever comes later.
- Baseline: 207 landing visitors, 19 registration starts, zero registration forms submitted, and zero trials as of 2026-07-31 10:09 UTC.
- Implementation: a four-action no-account demo, homepage and acquisition-page demo CTAs, dedicated demo events, and a separate demo funnel on the private growth dashboard.
- Decision rule: if visitors do not complete the demo, revise the workflow or message. If they complete but do not click through, revise the offer or trust proof. If they click but do not submit registration, fix signup friction. If registrations complete, move the next experiment to first-job activation.
