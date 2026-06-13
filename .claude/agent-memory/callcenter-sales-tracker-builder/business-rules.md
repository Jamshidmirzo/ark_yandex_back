---
name: naffAI business rule defaults
description: Locked product decisions and the defaults chosen autonomously
type: project
---

Defaults the team lead has NOT yet confirmed in writing — change these if/when she answers:

1. **Payroll formula:** `percent` with `payout_value=3.0` over `threshold=50,000,000 UZS/month`.
   Computed in `apps.payroll.services.compute_payout`. Engine also supports `fixed` and `tiers`
   (progressive). Per-operator override via `PayrollRule(scope='operator', operator=...)`.
2. **Trainees:** counted in shared totals/leaderboards by default; UI badges them and the
   `/api/payroll/monthly/?include_trainees=0` flag excludes them on demand.
3. **Returns:** excluded from operator credit and revenue/dashboards/leaderboard. Visible in
   `is_returned=true` filter and in Excel exports as a separate column. Selector
   `operator_sales_aggregate` filters `is_returned=False, is_deleted=False, status='confirmed'`.
4. **Gifts inside sale amount:** do NOT reduce the operator-credited amount. `GiftItem.cost`
   is only for future margin reports.
5. **Duplicate IMEI:** blocked by default; override requires BOTH `allow_duplicate_imei=true`
   AND a non-empty `duplicate_override_comment`. The comment is persisted to AuditLog.

Why: spec demanded sensible defaults documented in README «ПРИНЯТЫЕ ДОПУЩЕНИЯ»; all five are
listed there. The lead can revisit any of them by editing `PayrollRule` rows or env vars.

How to apply: before changing any of these, check whether the user/team lead has given a
concrete answer. If they have, update the relevant service + the README assumptions section
together so the docs stay truthful.
