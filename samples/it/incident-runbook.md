---
title: Incident Response Runbook
collection: it-runbooks
sensitivity: internal
---
# Severity levels

SEV1: customer-facing outage or data exposure, page the on-call engineer and the incident commander immediately. SEV2: degraded service, respond within 30 minutes. SEV3: minor issue, handle in business hours.

# First 15 minutes of a SEV1

Acknowledge the page, open an incident channel, assign an incident commander, and post a status update every 30 minutes. Check the deployment log first: most incidents follow a release.

# Database failover

The PostgreSQL primary fails over to the standby automatically when it is unreachable for 30 seconds. After a failover, confirm replication lag is under 5 seconds before reopening writes, then open a post-incident review.
