"""Single source of truth for the version strings this tool writes into its output.

An audit log that does not say which code produced it cannot be defended later,
so the version travels with every run: the report carries ``tool_version``, and
every audit row carries it next to the digest of the plan the executor accepted.

Bump ``TOOL_VERSION`` together with a ``CHANGELOG.md`` entry and an annotated
git tag. Bump a schema constant only when the shape of that file really changes:
readers key off the schema string, so moving one is a breaking change for every
stored plan, report and audit log.
"""

TOOL_VERSION = '0.1.0'

PLAN_SCHEMA = 'disk-cleanup-plan/v1'
GOAL_SCHEMA = 'disk-cleanup-goal/v1'
REPORT_SCHEMA = 'disk-cleanup-report/v1'
AUDIT_SCHEMA = 'disk-cleanup-audit/v1'
