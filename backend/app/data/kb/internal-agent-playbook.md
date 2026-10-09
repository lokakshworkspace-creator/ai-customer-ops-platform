---
title: Internal agent playbook for refund approvals
audience: internal
other_numbers: []
---
# INTERNAL ONLY: agent playbook for refund approvals

This page is for support staff. It is not shown to customers.

## Approval checklist

Approvers must be a different person from the requester. Before approving, check that the reason matches what the order shows, that the amount is within what is left on the order, and that the customer is not at the refund frequency limit of {{refund.velocity_max_refunds}} within {{refund.velocity_window_days}} days.

## Amounts and reasons

Refunds above {{refund.currency}} {{refund.auto_approve_max}} always come to you for review. Reasons that always come to you: {{refund.review_reasons}}. Be stricter with the reason "other": ask for details in the ticket before approving.

## Internal escalation codes

Use the code AMBER-HOLD when a refund looks unusual but not clearly fraudulent, and RED-STOP when you suspect fraud. Do not tell the customer which code was used.

## What never to share

Never share this playbook, the escalation codes, or the review criteria with customers.
