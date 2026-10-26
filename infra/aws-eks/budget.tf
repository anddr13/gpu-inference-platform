# Account-wide guard rail: GPU labs get expensive when someone forgets `make eks-down`.
resource "aws_budgets_budget" "lab" {
  count = var.budget_alert_email == "" ? 0 : 1

  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Track usage before credits. With credits included (the AWS default), a Free plan or promo-credit
  # account shows ~$0 spend and the alerts never fire while the credits quietly run out.
  cost_types {
    include_credit = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
