---
title: Refunds
audience: customer
other_numbers: ["3", "5"]
---
# Refunds

## How refunds are decided

Every refund request is checked by the shop's systems against fixed rules. The assistant cannot approve a refund by itself and cannot change the rules: it can only pass your request to the system, which gives a clear answer: approved, needs a person to approve it, or not allowed.

## Refunds that are paid right away

A refund of up to {{refund.currency}} {{refund.auto_approve_max}} is approved and paid immediately when the reason is one of: {{refund.auto_approve_reasons}}.

## Refunds that need a person to approve

A refund needs a person to review it when the amount is above {{refund.currency}} {{refund.auto_approve_max}}, when the reason is one of: {{refund.review_reasons}}, or when you have asked for more than {{refund.velocity_max_refunds}} refunds within {{refund.velocity_window_days}} days. A team member decides, and the assistant will tell you the result once it is known.

## Refunds that are not allowed

A refund is not allowed when the order is older than {{refund.max_order_age_days}} days, when the order was never paid, when the amount is more than the order total, or when it is more than what is left to refund on that order.

## When the money arrives

Approved refunds are sent back to the original payment method. It usually takes 3 to 5 business days to show on your statement.
