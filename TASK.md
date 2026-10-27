# TASK.md: working summary of the assignment

Source: "Alternative D: Business Data Investigator" (Junior AI Engineer take-home). This is the candidate's own summary, not the official text. `tasks/data/domain.md` remains the authority for metric rules.

Goal: a local investigation workspace for a fictional business whose reported sales changed. A model investigates a question through a few read-only SQL queries, uses the first result to choose a follow-up, and a reproducible report explains what the results establish and what remains unknown.

Time limit: one working day (up to 8 hours), including about 30-60 minutes of data preparation.

## Data preparation (current phase)
- Start from `tasks/data/` (`domain.md`, `schema.sql`, `seed.json`, `seed.sqlite`, `expected-seed-results.json`).
- Suggested expanded size: about 10 customers, 30 orders, 8 refunds (guide, not a strict target).
- Preserve customer, order and refund IDs and relationships. Keep the refund-driven net-sales change and the order with multiple refunds.
- Add rows that support a useful segment or period breakdown without changing the meaning of the metrics.
- Commit the final database, the generation method and the random seed (if any).
- Supplied expected values apply only to the original seed rows. Recalculate expectations for the expanded data independently of the application.
- Label simulated data. Report failed checks.

## Five reference cases (plan; expected values still to be computed independently)
| # | Case | What it checks |
|---|------|----------------|
| R1 | Main investigation, Aug -> Sep 2026 | gross, refunds, net for both periods; net change |
| R2 | Order O3 with two refunds | order amount counted once in gross |
| R3 | September breakdown by segment | segment totals add up to the period totals |
| R4 | Period boundary (rows dated 2026-10-01) | half-open periods: end excluded, start included |
| R5 | Refund in a later period than its order | refund counted by refund_date, segment taken from the order |

## Application requirements (later phases)
- Validate key relationships and amount units. Give the model the schema and the metric definitions.
- Read-only query tool returning columns, rows and a clear error. Keep every attempted query and its result or failure, linked to the triggering question.
- Model requests SQL through the tool and chooses at least one follow-up query based on the first result.
- Totals computed in SQL or application code; reported figures tied to executed results. State date ranges, units and assumptions.
- Explain how gross sales and refunds contribute to the net-sales change. Do not claim reasons for refunds; name the missing evidence.
- One investigation page: question input, concise explanation, a table or chart, inspectable SQL and result tables, follow-up question using the investigation's context, export or save of question, definitions, assumptions, queries, results and explanation.
- At most 6 query attempts per question (errors and retries count). Query time limit and result-size limit. On a limit, stop and show partial results or a failure.
- Read-only enforced by database or tool permissions. Writes rejected. Only the three local tables are accessible.
- Visible handling of malformed SQL, empty results and model/API failures. Save real responses, model/prompt configuration and query results for replay. Label incomplete investigations.
- Real programmatic model integration with saved real responses. Label cached responses and simulated failures. No credentials in the repository. Replay must work without an API key.

## Minimum demonstration (five checks, with expected vs observed and any unresolved failures)
1. The main investigation matches manually verified gross, refund and net totals for the selected periods.
2. The order with multiple refunds does not cause its gross sales to be counted more than once.
3. A follow-up breakdown stays consistent with the totals and includes its queries, result tables and assumptions.
4. A write attempt is rejected without changing the database, and a failed query counts toward the execution limit.
5. A saved report reproduces the refund discrepancy and distinguishes observed facts from an unsupported explanation of customer behaviour.

## Deliverables
- Git repository: solution, dependencies, small sample and replay files with no sensitive data.
- Dataset and starter material: seeds, additions, generation method, assumptions, five checked reference cases.
- README: setup and run, architecture, model configuration, data assumptions, time spent, known limitations, replay without an API key.
- Brief presentation (3-5 minute recording, a few slides, or a written walkthrough).
- LLM usage note: tools and models, generated components, one representative instruction or workflow, one correction or verification step.
- AI configuration: completed `ai-workflow/manifest.json` and `ai-workflow/README.md`, plus all exercise-specific skills, agents, hooks, prompts and settings; sanitized `.env.example`.
- Check results for the minimum demonstration.

## Out of scope
Live warehouses, semantic platforms, forecasting, external business actions, unrestricted SQL agents. Optional extras only after the required flow is finished.
