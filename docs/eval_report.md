# LexFlow eval report

| dataset | mode | docs | type_acc | field_acc | field_precision | field_recall | signer_recall | flag_f1 | route_acc | unsafe_auto | injection_caught | false_block | ms_per_doc |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| templated | rules | 64 | 1.00 | 0.99 | 1.00 | 0.98 | 1.00 | 0.99 | 1.00 | 0 | 5/5 | 0/59 | 2 |
| templated | hybrid | 64 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 5/5 | 0/59 | 28 |
| heldout | rules | 62 | 0.98 | 0.83 | 0.95 | 0.69 | 1.00 | 0.69 | 0.92 | 0 | 5/5 | 0/57 | 4 |
| heldout | hybrid | 62 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 5/5 | 0/57 | 39 |

Per-field accuracy:

| dataset | mode | parties | counterparty | effective_date | term_months | auto_renewal | renewal_notice_days | governing_law | liability_cap | payment_terms_days | contract_value | termination_for_convenience | confidentiality_years |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| templated | rules | 1.00 | 1.00 | 1.00 | 0.97 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.88 | 1.00 |
| templated | hybrid | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| heldout | rules | 0.60 | 0.60 | 0.94 | 0.66 | 0.94 | 1.00 | 1.00 | 0.98 | 0.76 | 0.84 | 0.63 | 0.98 |
| heldout | hybrid | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

LLM: {'calls': 0, 'cache_hits': 126, 'failures': 0, 'seconds': 0.0, 'budget_exhausted': 0}
