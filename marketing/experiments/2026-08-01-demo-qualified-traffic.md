# Experiment: Qualified traffic to the 60-second demo

- Product: FieldBase
- Verified observation: Since the no-account demo deployed, 43 external landing visitors produced two demo views and zero starts. All observed traffic is direct/unattributed.
- Customer and entry point: Owner-led US low-voltage or field-service contractor coordinating job intake, assignments, closeout, and invoicing.
- Hypothesis: If five highly relevant contractors receive a personalized no-account demo invitation and Glenn publishes one founder-led LinkedIn post, then qualified demo starts will increase because the ask is concrete, low commitment, and attributable.
- Primary metric: Distinct external demo completions / distinct external demo starts.
- Secondary metrics: Demo starts, demo-to-registration clicks, replies, registration submissions, and trials.
- Guardrails: No duplicate recipients from batch 01; public business contacts only; one email per prospect; opt-out included; no fabricated customer proof; no ad spend; no product changes during the test unless instrumentation fails.
- Minimum evidence or time window: 20 external demo starts or 14 days after demo deployment, whichever comes later.
- Baseline: 2 demo views, 0 starts, 0 completions, 0 demo-to-registration clicks.
- Implementation: Five personalized Gmail messages with unique `utm_content` values and one attributed founder LinkedIn post using an original FieldBase graphic.
- Result: Five attributed Gmail messages entered Sent at 2026-08-01 09:20 UTC. The attributed founder LinkedIn post published at 09:21 UTC and was verified at its public URL. A synthetic live journey passed all four demo steps and the registration redirect. One email-attributed and one LinkedIn-attributed view appeared within seconds without starting; treat both as probable automated link checks, not customer behavior. Customer response is pending.
- Confidence: High that the baseline is instrumented; low that either channel will create qualified traffic until observed.
- Decision: Continue until the gate. If recipients do not visit, revise targeting or subject/opening. If visitors view but do not start, revise the demo page or first action. If they start but do not finish, revise the workflow. If they finish but do not click through, revise trust or offer.
- Transferable learning: Pending.
